# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Tower of Fantasy's Perfect World launcher (no network or 7z needed)."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct, winegame
from nadare.backends.wine import _query_matches
from nadare.backends.winegame_parts import launching
from nadare.core import nte_launcher as tl
from nadare.core import sevenzip
from nadare.core.sources import tof

MZ = b"MZ" + b"\0" * 64
INSTALLER_BYTES = b"tof nsis installer"
REAL_LAUNCHER = b"MZ-hotta-launcher"
BOOTSTRAP_COPY = b"MZ-top-level-hotta-launcher"


@pytest.fixture(autouse=True)
def no_bundled_7zip(tmp_path, monkeypatch):
    """Unit tests must never pick up the real bundled 7-Zip a release build fetched."""
    monkeypatch.setattr(sevenzip, "BUNDLED", tmp_path / "no-bundled-7zzs")


@pytest.fixture(autouse=True)
def _pin_the_fake_installer(monkeypatch):
    monkeypatch.setattr(tl, "TOF_INSTALLER_SHA256", hashlib.sha256(INSTALLER_BYTES).hexdigest())


def _fake_7z(calls, *, inner=True, payload=True, launcher=True, nested_decoy=False, fail_at=None):
    """Pretend 7z: the NSIS installer holds WmGpLaunchSetupFiles.7z, which holds HottaLaunch/.

    ``nested_decoy`` puts a second HottaLaunch/ two levels down, not directly in the payload.
    ``fail_at`` makes that call (0-based) return a non-zero exit code, as a broken archive does.
    """

    def run(argv, **kw):
        calls.append(argv)
        if fail_at is not None and len(calls) - 1 == fail_at:
            return subprocess.CompletedProcess(argv, 2, "", "broken archive")
        outdir = Path(next(a for a in argv if a.startswith("-o"))[2:])
        archive = Path(argv[-1])  # [7z, "x", "-y", "-o<dir>", <archive>]
        if archive.suffix == ".exe":
            if inner:
                (outdir / "$PLUGINSDIR").mkdir(parents=True)
                (outdir / "WmGpLaunchSetupFiles.7z").write_bytes(b"inner-7z")
        else:
            (outdir / "HottaLauncher.exe").write_bytes(BOOTSTRAP_COPY)  # payload root copy
            (outdir / "uninst.exe").write_bytes(MZ)
            if payload:
                root = outdir / "HottaLaunch"
                (root / "Config").mkdir(parents=True)
                (root / "Config" / "Config.ini").write_text("x")
                (root / "HottaUpdate.exe").write_bytes(MZ)
                if launcher:
                    (root / "HottaLauncher.exe").write_bytes(REAL_LAUNCHER)
            if nested_decoy:
                deep = outdir / "Support" / "deep" / "HottaLaunch"
                deep.mkdir(parents=True)
                (deep / "HottaLauncher.exe").write_bytes(b"decoy")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _dl(urls, data=INSTALLER_BYTES):
    def download(url, dest):
        urls.append(url)
        Path(dest).write_bytes(data)

    return download


def test_tof_launcher_path():
    assert tl.tof_launcher_path(Path("/g/ToF")) == Path("/g/ToF/Launcher/HottaLauncher.exe")


def test_an_installed_launcher_is_left_alone(tmp_path):
    root = tmp_path / "ToF"
    exe = tl.tof_launcher_path(root)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(MZ)
    got = tl.ensure_tof_launcher(
        root,
        data_dir=tmp_path / "d",
        download=lambda *a: 1 / 0,
        which=lambda c: 1 / 0,
        run=lambda *a, **k: 1 / 0,
    )
    assert got == exe


def test_ensure_tof_launcher_unpacks_and_renames_hottalaunch_to_launcher(tmp_path):
    root = tmp_path / "ToF"
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "keep.txt").write_text("game")
    urls: list = []
    calls: list = []
    which = lambda c: "/usr/bin/7z" if c == "7z" else None  # noqa: E731
    got = tl.ensure_tof_launcher(
        root, data_dir=tmp_path / "d", download=_dl(urls), which=which, run=_fake_7z(calls)
    )
    assert got == root / "Launcher" / "HottaLauncher.exe"
    assert got.read_bytes() == REAL_LAUNCHER
    assert (root / "Launcher" / "Config" / "Config.ini").read_text() == "x"
    assert (root / "Launcher" / "HottaUpdate.exe").is_file()
    assert (root / "Client" / "keep.txt").read_text() == "game"
    assert not (root / "HottaLaunch").exists()
    # The payload's top-level bootstrap copy and uninstaller stay out of the game root.
    assert not (root / "HottaLauncher.exe").exists()
    assert not (root / "uninst.exe").exists()
    assert urls == [tl.TOF_INSTALLER_URL] and len(calls) == 2
    assert calls[0][0] == "/usr/bin/7z" and calls[0][1] == "x"
    assert (tmp_path / "d" / "tof" / "ToF_setup.exe").read_bytes() == INSTALLER_BYTES


def test_a_payload_without_hottalaunch_is_explained(tmp_path):
    with pytest.raises(tl.TofLauncherError, match="HottaLaunch"):
        tl.ensure_tof_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], payload=False),
        )
    assert not (tmp_path / "a" / "Launcher").exists()  # nothing half-installed


def test_a_nested_hottalaunch_decoy_is_not_the_payload_launcher(tmp_path):
    """The launcher must be a direct child of the payload, not one found deeper down."""
    with pytest.raises(tl.TofLauncherError, match="HottaLaunch"):
        tl.ensure_tof_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], payload=False, nested_decoy=True),
        )
    assert not (tmp_path / "a" / "Launcher").exists()


def test_a_launcher_without_the_exe_is_explained(tmp_path):
    with pytest.raises(tl.TofLauncherError, match="HottaLauncher.exe"):
        tl.ensure_tof_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], launcher=False),
        )


def test_a_missing_inner_archive_and_missing_7z_are_explained(tmp_path):
    with pytest.raises(tl.TofLauncherError, match="7-Zip.*Tower of Fantasy"):
        tl.ensure_tof_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d1",
            download=_dl([]),
            which=lambda c: None,
            run=_fake_7z([]),
        )
    with pytest.raises(tl.TofLauncherError, match="WmGpLaunchSetupFiles"):
        tl.ensure_tof_launcher(
            tmp_path / "b",
            data_dir=tmp_path / "d2",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], inner=False),
        )


@pytest.mark.parametrize("fail_at", [0, 1])
def test_a_failed_extraction_is_a_tof_error(tmp_path, fail_at):
    """Both 7-Zip runs (installer and payload) must report Tower of Fantasy's error type."""
    with pytest.raises(tl.TofLauncherError, match="cannot extract"):
        tl.ensure_tof_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], fail_at=fail_at),
        )
    assert not (tmp_path / "a" / "Launcher").exists()


@pytest.mark.parametrize("sha", ["", "not-a-hash", "a" * 63, "g" * 64])
def test_a_malformed_pin_refuses_before_download(tmp_path, monkeypatch, sha):
    monkeypatch.setattr(tl, "TOF_INSTALLER_SHA256", sha)
    calls: list = []
    with pytest.raises(tl.TofLauncherError, match="no valid checksum"):
        tl._fetch_tof_installer(tmp_path / "d" / "ToF_setup.exe", lambda *a: calls.append(a))
    assert calls == []


def test_a_wrong_installer_is_deleted_and_leaves_no_cache(tmp_path, monkeypatch):
    expected = b"expected installer"
    monkeypatch.setattr(tl, "TOF_INSTALLER_SHA256", hashlib.sha256(expected).hexdigest())
    cache = tmp_path / "d" / "ToF_setup.exe"

    with pytest.raises(tl.TofLauncherError, match="expected .*got"):
        tl._fetch_tof_installer(cache, _dl([], data=b"tampered"))
    assert not cache.exists()
    assert list(cache.parent.iterdir()) == []


def test_a_verified_installer_is_cached_between_runs(tmp_path):
    cache = tmp_path / "d" / "tof" / "ToF_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(INSTALLER_BYTES)
    assert tl._fetch_tof_installer(cache, lambda *a: pytest.fail("downloaded again")) == cache


class _Stop(RuntimeError):
    """A sentinel a patched hook raises, proving the hook was the one that ran."""


def test_ensure_launcher_uses_the_patchable_launcher_path(tmp_path, monkeypatch):
    """NTE's old ``launcher_path`` patch point still intercepts ``ensure_launcher``."""
    sentinel = tmp_path / "patched" / "NTEGlobalLauncher.exe"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(MZ)
    monkeypatch.setattr(tl, "launcher_path", lambda root: sentinel)
    assert (
        tl.ensure_launcher(
            tmp_path / "NTE",
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: None,
        )
        == sentinel
    )


def test_ensure_launcher_uses_the_patchable_fetch_installer(tmp_path, monkeypatch):
    """NTE's old ``_fetch_installer`` patch point still intercepts ``ensure_launcher``."""

    def fetch(cache, download):
        raise _Stop(str(cache))

    monkeypatch.setattr(tl, "_fetch_installer", fetch)
    with pytest.raises(_Stop, match="NTE_setup.exe"):
        tl.ensure_launcher(
            tmp_path / "NTE",
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: "/x/7z",
        )


def test_ensure_launcher_still_calls_extract_with_four_arguments(tmp_path, monkeypatch):
    """A four-argument ``_extract`` monkeypatch, as before ToF, must keep working."""
    seen = []

    def fetch(cache, download):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(INSTALLER_BYTES)
        return cache

    def extract(sevenz, archive, outdir, run):
        seen.append((sevenz, archive, outdir))
        raise _Stop()

    monkeypatch.setattr(tl, "_fetch_installer", fetch)
    monkeypatch.setattr(tl, "_extract", extract)
    with pytest.raises(_Stop):
        tl.ensure_launcher(
            tmp_path / "NTE",
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: "/x/7z",
        )
    assert seen and seen[0][0] == "/x/7z" and seen[0][2].name == "installer"


def test_ensure_tof_launcher_uses_the_patchable_tof_path(tmp_path, monkeypatch):
    sentinel = tmp_path / "patched" / "HottaLauncher.exe"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(MZ)
    monkeypatch.setattr(tl, "tof_launcher_path", lambda root: sentinel)
    assert (
        tl.ensure_tof_launcher(
            tmp_path / "ToF",
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: None,
        )
        == sentinel
    )


def test_ensure_tof_launcher_uses_the_patchable_tof_fetch(tmp_path, monkeypatch):
    def fetch(cache, download):
        raise _Stop(str(cache))

    monkeypatch.setattr(tl, "_fetch_tof_installer", fetch)
    with pytest.raises(_Stop, match="ToF_setup.exe"):
        tl.ensure_tof_launcher(
            tmp_path / "ToF",
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: "/x/7z",
        )


def _tof_backend(tmp_path, profile=None):
    pfx = tmp_path / "pfx"
    values = {"prefix": str(pfx)}
    values.update(profile or {})
    ctx = make_context(tmp_path, "direct-tof", values)
    backend = direct.TofBackend(ctx)
    game = backend.install_dir() / tof.EXE
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    return backend, game


def test_tof_starts_the_publisher_launcher(tmp_path, monkeypatch):
    backend, game = _tof_backend(tmp_path)
    exe = backend.install_dir() / "Launcher" / "HottaLauncher.exe"
    seen = {}

    def fake_ensure(root, *, data_dir, **kw):
        seen["root"], seen["data_dir"] = root, data_dir
        return exe

    monkeypatch.setattr(tl, "ensure_tof_launcher", fake_ensure)
    assert backend._direct_argv(game) == [str(exe), "/launcher", "/autoplay"]
    assert seen["root"] == backend.install_dir() and seen["data_dir"] == backend._data_dir()


def test_tof_forwards_the_players_launch_arguments(tmp_path, monkeypatch):
    backend, game = _tof_backend(tmp_path, {"launch_args": "-dx11 --windowed"})
    exe = backend.install_dir() / "Launcher" / "HottaLauncher.exe"
    monkeypatch.setattr(
        tl, "ensure_tof_launcher", lambda root, *, data_dir, **kw: exe
    )
    assert backend._direct_argv(game) == [
        str(exe),
        "/launcher",
        "/autoplay",
        "-dx11",
        "--windowed",
    ]


def test_tof_playing_tracks_the_game_not_the_launcher(tmp_path, monkeypatch):
    backend, game = _tof_backend(tmp_path)
    write_exe(backend.direct_target())
    monkeypatch.setattr(backend, "_runner_missing", lambda: False)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: False)
    monkeypatch.setattr(winegame.graphics, "vulkan_note", lambda: "")
    env = {"WINEPREFIX": str(backend.prefix() / "pfx")}

    def pids_for(*names):
        return lambda query: [1] if _query_matches(query, list(names), env) else []

    monkeypatch.setattr(winegame, "scan_pids", pids_for("HottaLauncher.exe"))
    assert not backend.status().running
    monkeypatch.setattr(winegame, "scan_pids", pids_for("QRSL.exe"))
    assert backend.status().running


def test_the_launcher_closes_after_qrsl_exits(tmp_path, monkeypatch):
    backend, game = _tof_backend(tmp_path)
    stopped = []
    monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
    seen_once = [False]

    def scan(query):
        assert query.names == ("QRSL.exe",) and query.require_name
        if not seen_once[0]:
            seen_once[0] = True
            return [1]
        return []

    monkeypatch.setattr(launching, "scan_pids", scan)
    now = [0.0]
    monkeypatch.setattr(launching.time, "monotonic", lambda: now[0])

    class Done:
        def wait(self, interval):
            now[0] += 1
            return now[0] > 6

    closed = threading.Event()
    backend._watch_game_process(Done(), closed)
    assert stopped == [True] and closed.is_set()


def test_tof_launcher_failure_is_a_backend_error(tmp_path, monkeypatch):
    backend, game = _tof_backend(tmp_path)

    def boom(*a, **k):
        raise tl.TofLauncherError("7-Zip not found")

    monkeypatch.setattr(tl, "ensure_tof_launcher", boom)
    with pytest.raises(direct.BackendError, match="cannot prepare the Tower of Fantasy launcher"):
        backend._direct_argv(game)


def test_tof_uses_xwayland_for_the_launcher_and_names_the_launcher_work():
    assert direct.TofBackend.default_display == "x11"
    summary = direct.TofBackend.page_summary
    notice = direct.TofBackend.page_notice
    assert "launcher" in summary
    assert "/autoplay" in notice[0] and "7-Zip" in notice[0]
    assert (
        "On the very first start the launcher updates itself once (a minute or two)."
        in notice[0]
    )
    # Plain user text: no internal executable names.
    assert ".exe" not in notice[0]
    # The game page needs ``(text, url[, label])``; a bare text tuple crashes its renderer.
    assert len(notice) in (2, 3) and notice[1].startswith("https://")
