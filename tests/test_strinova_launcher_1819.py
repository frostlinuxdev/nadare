# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for starting Strinova through its official launcher (#1819).

No network and no real 7-Zip: the installer is a fake archive whose extraction is
simulated by :func:`_fake_7z`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import direct, winegame
from nadare.backends.wine import _query_matches
from nadare.backends.winegame_parts import launching
from nadare.core import download, runners, sevenzip
from nadare.core import strinova_launcher as sl
from nadare.core.events import Cancel, Cancelled

MZ = b"MZ" + b"\0" * 64
INSTALLER_BYTES = b"strinova nsis installer"
LAUNCHER_BYTES = b"MZ-strinova-launcher"
GAME_VERSION = "1.12.1.2"


@pytest.fixture(autouse=True)
def no_bundled_7zip(tmp_path, monkeypatch):
    """Unit tests must never pick up the real bundled 7-Zip a release build fetched."""
    monkeypatch.setattr(sevenzip, "BUNDLED", tmp_path / "no-bundled-7zzs")


@pytest.fixture(autouse=True)
def _no_webview2_download(monkeypatch):
    """WebView2 before a launch is tested in test_strinova_webview2_1819; never fetch it here."""
    from nadare.backends import direct_strinova

    monkeypatch.setattr(direct_strinova.StrinovaBackend, "_ensure_webview2", lambda self: None)


@pytest.fixture(autouse=True)
def _pin_the_fake_installer(monkeypatch):
    monkeypatch.setattr(sl, "INSTALLER_SHA256", hashlib.sha256(INSTALLER_BYTES).hexdigest())


def _prefix(tmp_path):
    prefix = tmp_path / "pfx"
    game = prefix / "drive_c" / "Games" / "Strinova"
    game.mkdir(parents=True)
    (game / "Strinova.exe").write_bytes(MZ)
    return prefix, game


def _installed(prefix):
    """Put a launcher exe in place so the tests that are not about unpacking skip 7-Zip."""
    launcher = sl.launcher_path(prefix)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(MZ)
    return launcher


def _fake_7z(calls, *, launcher=True, fail=False):
    """Pretend 7z: the NSIS installer unpacks straight into the launcher directory."""

    def run(argv, **kw):
        calls.append(argv)
        if fail:
            return subprocess.CompletedProcess(argv, 2, "", "broken archive")
        outdir = Path(next(a for a in argv if a.startswith("-o"))[2:])
        (outdir / "$PLUGINSDIR").mkdir(parents=True)
        (outdir / "$PLUGINSDIR" / "InstallPlugin.dll").write_bytes(MZ)
        if launcher:
            (outdir / "Strinova.exe").write_bytes(LAUNCHER_BYTES)
            (outdir / "startup_launcher.exe").write_bytes(MZ)
            (outdir / "Core.dll").write_bytes(b"core")
            (outdir / "UpdateGUI").mkdir()
            (outdir / "UpdateGUI" / "UpdateGUI.exe").write_bytes(MZ)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _dl(urls, data=INSTALLER_BYTES):
    def download(url, dest):
        urls.append(url)
        Path(dest).write_bytes(data)

    return download


def _which(name):
    return "/usr/bin/7z" if name == "7z" else None


def _ensure(prefix, game, version=GAME_VERSION, **kw):
    kw.setdefault("download", lambda *a: pytest.fail("downloaded"))
    kw.setdefault("which", lambda c: pytest.fail("looked for 7-Zip"))
    kw.setdefault("run", lambda *a, **k: pytest.fail("ran 7-Zip"))
    return sl.ensure_launcher(
        prefix, game, version=version, data_dir=prefix.parent / "data", **kw
    )


def _install(prefix, data_dir, *, game_dir=None, version=GAME_VERSION, download=None, run=None):
    return sl.ensure_launcher(
        prefix,
        game_dir or prefix / "drive_c" / "Games" / "Strinova",
        version=version,
        data_dir=data_dir,
        download=download or _dl([]),
        which=_which,
        run=run or _fake_7z([]),
    )


def _general_block(path: Path, encoding: str = "utf-8") -> list[str]:
    """The non-blank lines of ``[General]``, in file order, from a CRLF file."""
    text = path.read_bytes().decode(encoding)
    lines = text.split("\r\n")
    assert lines[-1] == "", "the file must end with CRLF"
    start = lines.index("[General]")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
        len(lines),
    )
    return [line for line in lines[start + 1 : end] if line.strip()]


def _key(lines: list[str], name: str) -> str:
    return next(line.split("=", 1)[1] for line in lines if line.split("=", 1)[0] == name)


# ---- layout and the installer ------------------------------------------------


def test_paths_are_the_hand_tested_layout(tmp_path):
    prefix, _ = _prefix(tmp_path)
    assert sl.launcher_dir(prefix) == prefix / "drive_c" / "Strinova"
    assert sl.launcher_path(prefix) == prefix / "drive_c" / "Strinova" / "Strinova.exe"
    assert sl.game_link(prefix) == prefix / "drive_c" / "Strinova" / "Game"
    assert sl.config_path(prefix) == prefix / "drive_c" / "Strinova" / "config.ini"


def test_an_installed_launcher_is_left_alone_and_still_refreshed(tmp_path):
    prefix, game = _prefix(tmp_path)
    launcher = _installed(prefix)
    config = sl.config_path(prefix)
    config.write_bytes(b"[General]\r\nCurrentGameVersion=0.1\r\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, sl.game_link(prefix), target_is_directory=True)
    got = _ensure(prefix, game)
    assert got == launcher
    assert launcher.read_bytes() == MZ
    assert sl.game_link(prefix).resolve() == game.resolve()
    assert _key(_general_block(config), "CurrentGameVersion") == GAME_VERSION


def test_unpacks_and_installs_the_launcher_into_the_prefix(tmp_path):
    prefix, game = _prefix(tmp_path)
    urls: list = []
    calls: list = []
    got = sl.ensure_launcher(
        prefix,
        game,
        version=GAME_VERSION,
        data_dir=tmp_path / "d",
        download=_dl(urls),
        which=_which,
        run=_fake_7z(calls),
    )
    assert got == prefix / "drive_c" / "Strinova" / "Strinova.exe"
    assert got.read_bytes() == LAUNCHER_BYTES
    assert (got.parent / "startup_launcher.exe").is_file()
    assert (got.parent / "UpdateGUI" / "UpdateGUI.exe").is_file()
    assert not (got.parent / "$PLUGINSDIR").exists()
    # The game itself is not touched.
    assert (game / "Strinova.exe").read_bytes() == MZ
    assert urls == [sl.INSTALLER_URL]
    assert len(calls) == 1 and calls[0][0] == "/usr/bin/7z" and calls[0][1] == "x"
    assert calls[0][-1].endswith(".exe")
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    assert cache.read_bytes() == INSTALLER_BYTES
    assert not list(cache.parent.glob("*.part*"))


def test_an_interrupted_copy_leaves_no_launcher_and_installs_next_time(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    real_copy = shutil.copy2

    def failing_copy(src, dst, *a, **k):
        if Path(dst).name == "Core.dll":
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(sl.shutil, "copy2", failing_copy)
    with pytest.raises(sl.StrinovaLauncherError, match="cannot install"):
        _install(prefix, tmp_path / "d")
    # Strinova.exe is installed last, so an interrupted copy is retried, not skipped.
    assert not sl.launcher_path(prefix).exists()
    assert not list((prefix / "drive_c").glob(".Strinova*"))

    monkeypatch.setattr(sl.shutil, "copy2", real_copy)
    assert _install(prefix, tmp_path / "d").read_bytes() == LAUNCHER_BYTES


def test_a_dangling_launcher_symlink_is_replaced(tmp_path):
    prefix, game = _prefix(tmp_path)
    launcher = sl.launcher_path(prefix)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "missing", launcher)
    got = _install(prefix, tmp_path / "d")
    assert not got.is_symlink()
    assert got.read_bytes() == LAUNCHER_BYTES


def test_launcher_refuses_the_launcher_folder_and_paths_inside_it(tmp_path):
    prefix, _ = _prefix(tmp_path)
    for game_dir in (sl.launcher_dir(prefix), sl.launcher_dir(prefix) / "saved"):
        with pytest.raises(sl.StrinovaLauncherError, match="Strinova"):
            _install(prefix, tmp_path / "d", game_dir=game_dir, run=_fake_7z([]))
        assert not sl.launcher_path(prefix).exists()


def test_the_game_link_itself_may_be_the_install_dir(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    link.mkdir()
    (link / "Strinova.exe").write_bytes(MZ)
    assert _ensure(prefix, link).is_file()
    assert link.is_dir() and not link.is_symlink()


def test_launcher_refuses_a_prefix_that_does_not_exist(tmp_path):
    prefix = tmp_path / "bare"
    game = prefix / "drive_c" / "Games" / "Strinova"
    with pytest.raises(sl.StrinovaLauncherError, match="prefix"):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=_which,
            run=_fake_7z([]),
        )
    assert not (prefix / "drive_c").exists()


# ---- pinned download ---------------------------------------------------------


def test_the_pinned_installer_is_reused_from_the_cache(tmp_path):
    prefix, game = _prefix(tmp_path)
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(INSTALLER_BYTES)
    got = sl.ensure_launcher(
        prefix,
        game,
        version=GAME_VERSION,
        data_dir=tmp_path / "d",
        download=lambda *a: pytest.fail("downloaded again"),
        which=_which,
        run=_fake_7z([]),
    )
    assert got.is_file()
    assert cache.read_bytes() == INSTALLER_BYTES


def test_a_wrong_hash_cache_is_removed_and_downloaded_again(tmp_path):
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"stale installer")
    calls: list = []

    def download(url, dest):
        calls.append(url)
        Path(dest).write_bytes(INSTALLER_BYTES)

    assert sl._fetch_installer(cache, download) == cache
    assert calls == [sl.INSTALLER_URL]
    assert cache.read_bytes() == INSTALLER_BYTES


def test_uppercase_and_padded_pin_is_normalised(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sl, "INSTALLER_SHA256", "  " + hashlib.sha256(INSTALLER_BYTES).hexdigest().upper() + " "
    )
    cache = tmp_path / "d" / "Strinova_Launcher_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(INSTALLER_BYTES)
    assert sl._fetch_installer(cache, lambda *a: pytest.fail("downloaded again")) == cache


def test_a_wrong_installer_is_rejected_and_leaves_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "INSTALLER_SHA256", hashlib.sha256(b"expected").hexdigest())
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    with pytest.raises(sl.StrinovaLauncherError, match="SHA-256 mismatch"):
        sl._fetch_installer(cache, _dl([], data=b"tampered"))
    assert not cache.exists()
    assert not list(cache.parent.iterdir())


def test_a_failed_download_leaves_no_cache_or_parts(tmp_path):
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"

    def failing(url, dest):
        Path(dest).write_bytes(b"half")
        Path(str(dest) + ".part").write_bytes(b"half")
        raise download.DownloadError("offline")

    with pytest.raises(sl.StrinovaLauncherError, match="cannot download"):
        sl._fetch_installer(cache, failing)
    assert not cache.exists()
    assert not list(cache.parent.iterdir())


@pytest.mark.parametrize("obstacle", ["cache", "part"])
def test_a_cleanup_that_cannot_unlink_keeps_the_original_error(tmp_path, obstacle):
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    cache.parent.mkdir(parents=True)
    if obstacle == "cache":
        cache.mkdir()  # unlink() on a folder raises, masking the download error
    else:
        cache.with_name(cache.name + ".part").mkdir()

    def boom(url, dest):
        raise download.DownloadError("offline")

    with pytest.raises(sl.StrinovaLauncherError, match="cannot download"):
        sl._fetch_installer(cache, boom)


def test_a_stale_staged_launcher_symlink_is_not_followed(tmp_path):
    src = tmp_path / "payload"
    src.mkdir()
    (src / sl.LAUNCHER_EXE).write_bytes(LAUNCHER_BYTES)
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    staged = dest / (sl.LAUNCHER_EXE + ".nadare-tmp")
    staged.symlink_to(outside)

    sl._copy_payload(src, dest)

    assert outside.read_bytes() == b"keep"
    assert (dest / sl.LAUNCHER_EXE).read_bytes() == LAUNCHER_BYTES
    assert not os.path.lexists(staged)


def test_a_failed_launcher_replace_removes_the_staged_copy(tmp_path, monkeypatch):
    src = tmp_path / "payload"
    src.mkdir()
    (src / sl.LAUNCHER_EXE).write_bytes(LAUNCHER_BYTES)
    dest = tmp_path / "dest"
    dest.mkdir()

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(sl.os, "replace", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="cannot install"):
        sl._copy_payload(src, dest)
    assert not os.path.lexists(dest / (sl.LAUNCHER_EXE + ".nadare-tmp"))


def test_the_default_download_gets_the_pin_size_emit_and_cancel(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    seen: dict = {}

    def fake_download(url, dest, **kw):
        seen.update({"url": url, "dest": Path(dest), **kw})
        Path(dest).write_bytes(INSTALLER_BYTES)

    monkeypatch.setattr(sl._dl, "download", fake_download)
    emit = lambda event: None  # noqa: E731
    cancel = Cancel()
    sl.ensure_launcher(
        prefix,
        game,
        version=GAME_VERSION,
        data_dir=tmp_path / "d",
        which=_which,
        run=_fake_7z([]),
        emit=emit,
        cancel=cancel,
    )
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    assert seen["url"] == sl.INSTALLER_URL
    assert seen["dest"] == cache
    assert seen["expected_sha256"] == hashlib.sha256(INSTALLER_BYTES).hexdigest()
    assert seen["expected_size"] == sl.INSTALLER_SIZE
    assert seen["emit"] is emit and seen["cancel"] is cancel
    # One fixed staging name, not a random mkstemp name under it.
    assert not list(cache.parent.glob("*.part*"))


@pytest.mark.parametrize("sha", ["", "not-a-hash", "a" * 63, "g" * 64])
def test_a_malformed_pin_refuses_before_download(tmp_path, monkeypatch, sha):
    monkeypatch.setattr(sl, "INSTALLER_SHA256", sha)
    calls: list = []
    with pytest.raises(sl.StrinovaLauncherError, match="no valid checksum"):
        sl._fetch_installer(tmp_path / "d" / "setup.exe", lambda *a: calls.append(a))
    assert calls == []


# ---- errors are plain --------------------------------------------------------


def test_a_download_error_is_a_plain_error(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)

    def boom(url, dest, **kw):
        raise download.DownloadError("cannot reach the server")

    monkeypatch.setattr(sl._dl, "download", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="cannot download"):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            which=_which,
            run=_fake_7z([]),
        )


def test_an_http_error_is_a_plain_error(tmp_path):
    prefix, game = _prefix(tmp_path)

    def boom(url, dest):
        raise download.HTTPStatusError(
            "404 Not Found", status=404, url=url
        )

    with pytest.raises(sl.StrinovaLauncherError, match="cannot download"):
        _install(prefix, tmp_path / "d", download=boom)


@pytest.mark.parametrize("error", [OSError("no 7z"), subprocess.SubprocessError("broken pipe")])
def test_a_7zip_failure_is_a_plain_error(tmp_path, error):
    prefix, game = _prefix(tmp_path)

    def run(*a, **k):
        raise error

    with pytest.raises(sl.StrinovaLauncherError, match="cannot extract"):
        _install(prefix, tmp_path / "d", run=run)
    assert not sl.launcher_path(prefix).exists()


@pytest.mark.parametrize(
    "error", [OSError("disk full"), shutil.Error([("src", "dst", "copy failed")])]
)
def test_a_filesystem_error_is_a_plain_error(tmp_path, monkeypatch, error):
    prefix, game = _prefix(tmp_path)

    def boom(*a, **k):
        raise error

    monkeypatch.setattr(sl.shutil, "copy2", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="cannot install"):
        _install(prefix, tmp_path / "d")


def test_a_link_error_is_a_plain_error(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)

    def boom(*a, **k):
        raise OSError("read-only drive_c")

    monkeypatch.setattr(sl.os, "symlink", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="Game"):
        _ensure(prefix, game)


def test_a_config_write_error_is_a_plain_error(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(sl.os, "replace", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="config.ini"):
        sl._merge_config(path, GAME_VERSION)


def test_missing_7zip_is_a_plain_error(tmp_path):
    prefix, game = _prefix(tmp_path)
    with pytest.raises(sl.StrinovaLauncherError, match="7-Zip.*Strinova"):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=lambda *a: pytest.fail("downloaded"),
            which=lambda c: None,
        )
    assert not sl.launcher_path(prefix).exists()


def test_a_failed_extraction_is_a_launcher_error(tmp_path):
    prefix, game = _prefix(tmp_path)
    with pytest.raises(sl.StrinovaLauncherError, match="cannot extract"):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=_which,
            run=_fake_7z([], fail=True),
        )
    assert not sl.launcher_path(prefix).exists()


def test_a_payload_without_the_launcher_is_explained(tmp_path):
    prefix, game = _prefix(tmp_path)
    with pytest.raises(sl.StrinovaLauncherError, match="Strinova.exe"):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=_which,
            run=_fake_7z([], launcher=False),
        )
    assert not sl.launcher_path(prefix).exists()


def test_the_default_which_and_run_are_resolved_at_call_time(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    looked: list = []
    calls: list = []

    def fake_which(name):
        looked.append(name)
        return "/fake/7z" if name == "7z" else None

    fake_run = _fake_7z(calls)
    monkeypatch.setattr(sl.shutil, "which", fake_which)
    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    got = sl.ensure_launcher(
        prefix, game, version=GAME_VERSION, data_dir=tmp_path / "d", download=_dl([])
    )
    assert got.is_file()
    assert "7z" in looked
    assert calls and calls[0][0] == "/fake/7z"


# ---- the first Play ----------------------------------------------------------


def test_the_first_play_says_it_is_downloading_then_installing(tmp_path):
    prefix, game = _prefix(tmp_path)
    order: list[str] = []

    def download(url, dest):
        order.append("download")
        Path(dest).write_bytes(INSTALLER_BYTES)

    def run(argv, **kw):
        order.append("unpack")
        return _fake_7z([])(argv, **kw)

    def emit(event):
        if event.kind == "step":
            order.append(event.message.lower())

    sl.ensure_launcher(
        prefix,
        game,
        version=GAME_VERSION,
        data_dir=tmp_path / "d",
        download=download,
        which=_which,
        run=run,
        emit=emit,
    )
    said_download = next(i for i, line in enumerate(order) if "download" in line)
    said_install = next(i for i, line in enumerate(order) if "install" in line)
    assert said_download < order.index("download")
    assert said_install < order.index("unpack")


def test_a_cancel_after_the_download_stops_before_unpacking(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    cancel = Cancel()
    ran: list = []

    def download(url, dest):
        Path(dest).write_bytes(INSTALLER_BYTES)
        cancel.cancel()

    # The hash itself does not see the cancel: the explicit check after it must.
    monkeypatch.setattr(
        sl, "_sha256", lambda path, cancel=None: hashlib.sha256(INSTALLER_BYTES).hexdigest()
    )
    with pytest.raises(Cancelled):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=download,
            which=_which,
            run=_fake_7z(ran),
            cancel=cancel,
        )
    assert ran == []
    assert not sl.launcher_path(prefix).exists()


def test_a_cancel_after_the_unpack_stops_before_copying(tmp_path):
    prefix, game = _prefix(tmp_path)
    cancel = Cancel()
    calls: list = []

    def run(argv, **kw):
        result = _fake_7z(calls)(argv, **kw)
        cancel.cancel()
        return result

    with pytest.raises(Cancelled):
        sl.ensure_launcher(
            prefix,
            game,
            version=GAME_VERSION,
            data_dir=tmp_path / "d",
            download=_dl([]),
            which=_which,
            run=run,
            cancel=cancel,
        )
    assert not sl.launcher_path(prefix).exists()
    assert not list((prefix / "drive_c").glob(".Strinova*"))


def test_a_cancel_during_the_cache_hash_stops(tmp_path):
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(INSTALLER_BYTES)
    cancel = Cancel()
    cancel.cancel()

    with pytest.raises(Cancelled):
        sl._fetch_installer(cache, lambda *a: pytest.fail("downloaded"), cancel)
    # Verifying the 144 MiB cache honours Stop, and the verified-looking file stays.
    assert cache.read_bytes() == INSTALLER_BYTES


def test_a_cancel_during_the_download_keeps_the_part_file(tmp_path):
    cache = tmp_path / "d" / "strinova" / "Strinova_Launcher_setup.exe"
    part = cache.with_name(cache.name + ".part")

    def download(url, dest):
        part.write_bytes(b"half of the installer")
        raise Cancelled()

    with pytest.raises(Cancelled):
        sl._fetch_installer(cache, download)
    # The next Play resumes from these bytes; a cancel is not a failed download.
    assert part.read_bytes() == b"half of the installer"
    assert not cache.exists()


def test_a_cancel_is_not_wrapped_by_ensure_launcher(tmp_path):
    prefix, game = _prefix(tmp_path)

    def download(url, dest):
        raise Cancelled()

    with pytest.raises(Cancelled):
        _install(prefix, tmp_path / "d", download=download)


# ---- the Game link -----------------------------------------------------------


def test_the_game_folder_is_linked_into_the_launcher(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    _ensure(prefix, game)
    link = sl.game_link(prefix)
    assert link.is_symlink()
    # A relative link: moving the prefix to another disk must not point at the old one.
    assert not Path(os.readlink(link)).is_absolute()
    assert link.resolve() == game.resolve()


def test_an_inside_game_folder_gets_a_relative_link(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    _ensure(prefix, game)
    assert os.readlink(sl.game_link(prefix)) == os.path.relpath(
        game, sl.game_link(prefix).parent
    )


def test_an_outside_game_folder_is_linked_by_its_resolved_path(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    external = tmp_path / "external"
    external.mkdir()
    (external / "Strinova.exe").write_bytes(MZ)
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    _ensure(prefix, alias)
    assert Path(os.readlink(sl.game_link(prefix))) == external.resolve()


def test_a_relative_game_dir_is_made_absolute(tmp_path, monkeypatch):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    external = tmp_path / "external"
    external.mkdir()
    (external / "Strinova.exe").write_bytes(MZ)
    monkeypatch.chdir(tmp_path)
    _ensure(prefix, Path("external"))
    target = Path(os.readlink(sl.game_link(prefix)))
    assert target.is_absolute()
    assert target.resolve() == external.resolve()


def test_a_link_with_the_wanted_target_is_left_alone(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    _ensure(prefix, game)
    link = sl.game_link(prefix)
    before = os.readlink(link)
    _ensure(prefix, game)
    assert os.readlink(link) == before


def test_an_existing_link_in_another_form_is_upgraded(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    os.symlink(game.resolve(), link, target_is_directory=True)  # absolute, not the wanted form
    assert Path(os.readlink(link)).is_absolute()
    _ensure(prefix, game)
    assert os.readlink(link) == os.path.relpath(game.resolve(), link.parent.resolve())
    assert not Path(os.readlink(link)).is_absolute()


def _aliased_prefix(tmp_path):
    """A prefix reached through a symlinked parent (``/home -> /var/home`` style)."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    game = real / "pfx" / "drive_c" / "Games" / "Strinova"
    game.mkdir(parents=True)
    (game / "Strinova.exe").write_bytes(MZ)
    return alias / "pfx", game


def test_a_symlinked_prefix_gets_a_link_inside_the_prefix(tmp_path):
    prefix, game = _aliased_prefix(tmp_path)
    _installed(prefix)
    _ensure(prefix, game)  # the resolved game path, the aliased prefix
    link = sl.game_link(prefix)
    assert os.readlink(link) == os.path.relpath(game.resolve(), link.parent.resolve())
    assert not Path(os.readlink(link)).is_absolute()
    assert link.resolve() == game.resolve()


def test_an_adopted_game_folder_at_the_link_is_accepted_on_a_symlinked_prefix(tmp_path):
    prefix, _ = _aliased_prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    link.mkdir()
    (link / "Strinova.exe").write_bytes(MZ)
    # The install dir as another tool reports it: resolved, while the prefix is aliased.
    assert _ensure(prefix, link.resolve()).is_file()
    assert link.is_dir() and not link.is_symlink()


def test_a_subfolder_of_the_game_link_is_refused(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    (link / "sub").mkdir(parents=True)
    with pytest.raises(sl.StrinovaLauncherError, match="inside"):
        _ensure(prefix, link / "sub")
    assert (link / "sub").is_dir()


def _symlinked_link(prefix, target):
    """Point the prefix's ``Game`` link at ``target`` the way a first Play leaves it."""
    link = sl.game_link(prefix)
    os.symlink(target, link, target_is_directory=True)
    return link


def test_a_subfolder_of_a_symlinked_game_link_is_refused(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    target = tmp_path / "Games" / "Strinova"
    target.mkdir(parents=True)
    (target / "sub").mkdir()
    link = _symlinked_link(prefix, os.path.relpath(target, sl.game_link(prefix).parent))
    with pytest.raises(sl.StrinovaLauncherError, match="inside"):
        _ensure(prefix, link / "sub")
    assert (target / "sub").is_dir()
    assert link.is_symlink() and link.resolve() == target.resolve()


def test_a_subfolder_of_an_outside_game_link_is_refused(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    outside = tmp_path / "outside" / "Strinova"
    outside.mkdir(parents=True)
    (outside / "sub").mkdir()
    link = _symlinked_link(prefix, outside)
    with pytest.raises(sl.StrinovaLauncherError, match="inside"):
        _ensure(prefix, link / "sub")
    assert os.readlink(link) == str(outside)
    assert (outside / "sub").is_dir()


@pytest.mark.parametrize("alias_side", ["prefix", "game"])
def test_a_subfolder_of_the_game_link_is_refused_across_an_aliased_parent(
    tmp_path, alias_side
):
    # /home vs /var/home: the prefix and the game folder name the same place by two routes.
    real, alias = tmp_path / "real", tmp_path / "alias"
    (real / "pfx").mkdir(parents=True)
    alias.symlink_to(real, target_is_directory=True)
    _installed(real / "pfx")
    outside = tmp_path / "outside" / "Strinova"
    (outside / "sub").mkdir(parents=True)
    link = _symlinked_link(real / "pfx", outside)
    prefix, game_root = (alias, real) if alias_side == "prefix" else (real, alias)
    game_dir = game_root / link.relative_to(real) / "sub"
    with pytest.raises(sl.StrinovaLauncherError, match="inside"):
        _ensure(prefix / "pfx", game_dir)
    assert os.readlink(link) == str(outside)
    assert (outside / "sub").is_dir()


def test_a_game_link_into_the_launcher_folder_allows_no_overlap(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    launcher = sl.launcher_dir(prefix)
    (launcher / "sub").mkdir()
    link = _symlinked_link(prefix, launcher)
    for game_dir in (launcher, launcher / "sub", link / "sub"):
        with pytest.raises(sl.StrinovaLauncherError, match="inside"):
            _ensure(prefix, game_dir)
    assert os.readlink(link) == str(launcher)


def test_a_game_link_to_a_subfolder_of_the_launcher_allows_no_overlap(tmp_path):
    prefix, _ = _prefix(tmp_path)
    _installed(prefix)
    inner = sl.launcher_dir(prefix) / "inner"
    inner.mkdir()
    link = _symlinked_link(prefix, inner)
    for game_dir in (inner, link / "sub"):
        with pytest.raises(sl.StrinovaLauncherError, match="inside"):
            _ensure(prefix, game_dir)
    assert os.readlink(link) == str(inner)


def test_a_wrong_game_link_is_repaired(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = sl.game_link(prefix)
    os.symlink(elsewhere, link, target_is_directory=True)
    _ensure(prefix, game)
    assert link.resolve() == game.resolve()


def test_a_broken_game_link_is_repaired(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    os.symlink(tmp_path / "missing", link, target_is_directory=True)
    _ensure(prefix, game)
    assert link.resolve() == game.resolve()


def test_a_failed_link_replacement_keeps_the_old_link(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    old = tmp_path / "old"
    old.mkdir()
    link = sl.game_link(prefix)
    os.symlink(old, link, target_is_directory=True)

    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(sl.os, "symlink", boom)
    with pytest.raises(sl.StrinovaLauncherError, match="Game"):
        _ensure(prefix, game)
    assert link.is_symlink()
    assert link.resolve() == old.resolve()


def test_a_real_game_folder_is_not_deleted(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    link = sl.game_link(prefix)
    link.mkdir()
    marker = link / "keep.bin"
    marker.write_bytes(b"user data")
    with pytest.raises(sl.StrinovaLauncherError, match="Game"):
        _ensure(prefix, game)
    assert marker.read_bytes() == b"user data"


# ---- config.ini --------------------------------------------------------------


def test_config_is_created_for_a_fresh_launcher(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    assert not path.exists()
    _ensure(prefix, game, version=GAME_VERSION)
    block = _general_block(path)
    assert _key(block, "CurrentGameVersion") == GAME_VERSION
    assert _key(block, "TargetGameVersion") == GAME_VERSION
    assert _key(block, "bPatchFinished") == "true"
    assert len(block) == 3


def test_a_zero_byte_config_gets_crlf_inside_general(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"")
    _ensure(prefix, game)
    raw = path.read_bytes()
    assert raw.startswith(b"[General]\r\n")
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert b"\r" not in raw.replace(b"\r\n", b"")
    assert _key(_general_block(path), "bPatchFinished") == "true"


def test_config_keeps_what_the_launcher_wrote(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(
        b"; launcher wrote this\r\n"
        b"[General]\r\n"
        b"IsAutoStart=false\r\n"
        b"CurrentGameVersion=0.1\r\n"
        b"GamePath=C:/Strinova/Game\r\n"
        b"\r\n"
        b"[Other]\r\n"
        b"Keep=1\r\n"
    )
    _ensure(prefix, game, version=GAME_VERSION)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    assert "; launcher wrote this" in text
    assert "IsAutoStart=false" in text
    assert "GamePath=C:/Strinova/Game" in text
    assert "[Other]" in text and "Keep=1" in text
    block = _general_block(path)
    assert _key(block, "CurrentGameVersion") == GAME_VERSION
    assert _key(block, "TargetGameVersion") == GAME_VERSION
    assert _key(block, "bPatchFinished") == "true"
    assert text.count("CurrentGameVersion") == 1
    # The launcher's file is CRLF; keep it that way.
    assert b"[Other]\r\nKeep=1" in raw


def test_new_keys_go_into_general_not_the_next_section(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"[General]\nIsAutoStart=false\n\n[Other]\nKeep=1\n")
    _ensure(prefix, game, version=GAME_VERSION)
    block = _general_block(path)
    assert _key(block, "TargetGameVersion") == GAME_VERSION
    assert _key(block, "bPatchFinished") == "true"
    # Every line of the file is CRLF, including the rewritten LF input.
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    assert b"[Other]\r\nKeep=1" in path.read_bytes()


def test_config_versions_follow_the_installed_game(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    _ensure(prefix, game, version="1.12.1.2")
    _ensure(prefix, game, version="1.12.2.0")
    block = _general_block(sl.config_path(prefix))
    assert _key(block, "CurrentGameVersion") == "1.12.2.0"
    assert _key(block, "TargetGameVersion") == "1.12.2.0"
    assert "1.12.1.2" not in "\r\n".join(block)


def test_config_duplicate_keys_are_all_updated(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(
        b"[General]\r\n"
        b"CurrentGameVersion=old\r\n"
        b"CurrentGameVersion=older\r\n"
        b"TargetGameVersion=old\r\n"
        b"bPatchFinished=false\r\n"
    )
    _ensure(prefix, game, version=GAME_VERSION)
    text = path.read_text(encoding="utf-8")
    assert "old" not in text
    assert text.count(f"CurrentGameVersion={GAME_VERSION}") == 2
    assert text.count(f"TargetGameVersion={GAME_VERSION}") == 1
    assert "bPatchFinished=true" in text


def test_config_utf8_bom_is_kept(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"\xef\xbb\xbf[General]\r\nOther=1\r\n")
    _ensure(prefix, game)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "Other=1" in text and text.count("[General]") == 1
    assert _key(_general_block(path, "utf-8-sig"), "CurrentGameVersion") == GAME_VERSION


def test_config_utf16_bom_is_kept(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes("[General]\r\nOther=1\r\n".encode("utf-16"))
    _ensure(prefix, game)
    raw = path.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16")
    assert "Other=1" in text and text.count("[General]") == 1
    assert "bPatchFinished=true" in text


def test_config_latin1_stays_latin1(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"[General]\r\nCaf\xe9=1\r\n")
    _ensure(prefix, game)
    raw = path.read_bytes()
    assert b"Caf\xe9=1" in raw
    assert b"Caf\xc3\xa9" not in raw


@pytest.mark.parametrize("bad", ["", "   ", "1.2\n3", "1.2\r3"])
def test_config_rejects_a_malformed_version(tmp_path, bad):
    with pytest.raises(sl.StrinovaLauncherError, match="version"):
        sl._merge_config(tmp_path / "config.ini", bad)
    assert not (tmp_path / "config.ini").exists()


def test_config_only_splits_on_real_line_breaks(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    # 0x85 is latin-1 text (…), not a line break: str.splitlines() would cut it.
    path.write_bytes(b"[General]\r\nCaf\xe9=one\x85two\r\n")
    _ensure(prefix, game)
    assert b"Caf\xe9=one\x85two" in path.read_bytes()


def test_a_version_that_cannot_be_encoded_is_a_plain_error(tmp_path):
    path = tmp_path / "config.ini"
    path.write_bytes(b"[General]\r\nCaf\xe9=1\r\n")
    before = path.read_bytes()
    with pytest.raises(sl.StrinovaLauncherError, match="cannot write"):
        sl._merge_config(path, "版本 1.0")
    assert path.read_bytes() == before


def test_config_without_general_gets_one_after_the_other_section(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"[Other]\r\nKeep=1\r\n")
    _ensure(prefix, game)
    text = path.read_text(encoding="utf-8")
    assert b"[Other]\r\nKeep=1\r\n\r\n[General]\r\n" in path.read_bytes()
    assert "[General]" in text
    assert text.index("[Other]") < text.index("[General]")
    assert _key(_general_block(path), "bPatchFinished") == "true"


def test_config_matches_a_lowercase_header_and_keys(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(
        b"[general]\r\n"
        b"CURRENTGAMEVERSION=old\r\n"
        b"targetgameversion=old\r\n"
        b"BPatchFinished=false\r\n"
    )
    _ensure(prefix, game)
    text = path.read_text(encoding="utf-8")
    assert text.count("[general]") + text.count("[General]") == 1
    assert text.count("CurrentGameVersion") == 1  # the rewritten line, now canonical
    assert "CURRENTGAMEVERSION=" not in text
    assert "targetgameversion=" not in text
    assert "BPatchFinished=" not in text
    assert f"CurrentGameVersion={GAME_VERSION}" in text
    assert f"TargetGameVersion={GAME_VERSION}" in text
    assert "bPatchFinished=true" in text


def test_config_utf16_be_bom_is_kept(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"\xfe\xff" + "[General]\r\nOther=1\r\n".encode("utf-16-be"))
    _ensure(prefix, game)
    raw = path.read_bytes()
    assert raw.startswith(b"\xfe\xff")
    text = raw[2:].decode("utf-16-be")
    assert "Other=1" in text and "bPatchFinished=true" in text


def test_a_missing_version_leaves_the_config_alone(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    path = sl.config_path(prefix)
    path.write_bytes(b"[General]\r\nCurrentGameVersion=1.0\r\n")
    _ensure(prefix, game, version=None)
    assert path.read_bytes() == b"[General]\r\nCurrentGameVersion=1.0\r\n"


def test_a_missing_version_does_not_create_a_config(tmp_path):
    prefix, game = _prefix(tmp_path)
    _installed(prefix)
    assert not sl.config_path(prefix).exists()
    _ensure(prefix, game, version=None)
    assert not sl.config_path(prefix).exists()


# ---- the backend -------------------------------------------------------------


def _backend(tmp_path, profile=None):
    values = {"prefix": str(tmp_path / "pfx")}
    values.update(profile or {})
    ctx = make_context(tmp_path, "direct-strinova", values)
    backend = direct.StrinovaBackend(ctx)
    game = backend.install_dir() / "Strinova.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(MZ)
    (backend.install_dir() / ".nadare-version.json").write_text(
        json.dumps({"version": GAME_VERSION, "game_tag": "strinova"}), encoding="utf-8"
    )
    return backend, game


def test_strinova_starts_the_official_launcher(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path)
    exe = backend.prefix() / "drive_c" / "Strinova" / "Strinova.exe"
    seen = {}

    def fake_ensure(prefix, game_dir, *, version, data_dir, **kw):
        seen.update(prefix=prefix, game_dir=game_dir, version=version, data_dir=data_dir, **kw)
        return exe

    monkeypatch.setattr(sl, "ensure_launcher", fake_ensure)
    assert backend._direct_argv(game) == [str(exe)]
    assert seen["prefix"] == backend.prefix()
    assert seen["game_dir"] == backend.install_dir()
    assert seen["version"] == GAME_VERSION
    assert seen["data_dir"] == backend._data_dir()
    assert seen["emit"] is backend.ctx.emit
    assert seen["cancel"] is backend.ctx.cancel


def test_strinova_keeps_the_players_launch_arguments_off_the_launcher(
    tmp_path, monkeypatch
):
    backend, game = _backend(tmp_path, {"launch_args": "-dx11 --windowed"})
    events: list = []
    backend.ctx.emit = events.append
    exe = backend.prefix() / "drive_c" / "Strinova" / "Strinova.exe"
    monkeypatch.setattr(sl, "ensure_launcher", lambda *a, **k: exe)
    assert backend._direct_argv(game) == [str(exe)]
    assert backend._direct_argv(game) == [str(exe)]
    notices = [
        event.message
        for event in events
        if event.kind == "log" and "ignored" in event.message.lower()
    ]
    assert len(notices) == 1


def test_a_missing_version_is_said_and_the_launcher_still_starts(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path)
    (backend.install_dir() / ".nadare-version.json").unlink()
    events: list = []
    backend.ctx.emit = events.append
    seen = {}
    exe = backend.prefix() / "drive_c" / "Strinova" / "Strinova.exe"

    def fake_ensure(prefix, game_dir, *, version, data_dir, **kw):
        seen["version"] = version
        return exe

    monkeypatch.setattr(sl, "ensure_launcher", fake_ensure)
    assert backend._direct_argv(game) == [str(exe)]
    assert seen["version"] is None
    assert any("may offer" in event.message for event in events)


def test_strinova_tracks_the_game_not_the_launcher(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path)
    monkeypatch.setattr(backend, "_runner_missing", lambda: False)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: False)
    monkeypatch.setattr(winegame.graphics, "vulkan_note", lambda: "")
    env = {"WINEPREFIX": str(backend.prefix() / "pfx")}

    def pids_for(*names):
        return lambda query: [1] if _query_matches(query, list(names), env) else []

    monkeypatch.setattr(winegame, "scan_pids", pids_for("Strinova.exe"))
    assert not backend.status().running
    monkeypatch.setattr(
        winegame, "scan_pids", pids_for("Strinova-Win64-Shipping.exe")
    )
    assert backend.status().running


def test_the_launcher_closes_after_the_game_exits(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path)
    stopped = []
    monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
    seen_once = [False]

    def scan(query):
        assert query.names == ("Strinova-Win64-Shipping.exe",) and query.require_name
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


def test_strinova_runs_on_x11_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    backend, _ = _backend(tmp_path)
    assert direct.StrinovaBackend.default_display == "x11"
    assert backend._wayland_default() == "0"
    assert backend._display_in_use() == "x11"


def test_strinova_can_still_choose_wayland(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    backend, _ = _backend(tmp_path, {"display": "wayland"})
    assert backend._wayland_default() == "1"
    assert backend._display_in_use() == "wayland"


def _no_launcher_process(backend, monkeypatch):
    """Play and its exit look for the publisher launcher; the test host never has one."""
    monkeypatch.setattr(backend, "_launcher_running", lambda: False)


def test_the_launcher_is_prepared_after_the_prefix_exists(tmp_path, monkeypatch):
    # A library install outside the prefix: nothing has created prefix/drive_c yet.
    backend, game = _backend(
        tmp_path, {"install_dir": str(tmp_path / "library" / "Strinova")}
    )
    assert not (backend.prefix() / "drive_c").exists()
    seen: list = []
    monkeypatch.setattr(
        backend, "_ensure_default_runner", lambda *, strict: seen.append("runner")
    )

    def ensure_prefix():
        seen.append("prefix")
        (backend.prefix() / "drive_c").mkdir(parents=True)
        return True

    monkeypatch.setattr(backend, "_ensure_prefix", ensure_prefix)
    exe = backend.prefix() / "drive_c" / "Strinova" / "Strinova.exe"

    def fake_ensure(prefix, game_dir, **kw):
        assert (backend.prefix() / "drive_c").is_dir()
        seen.append("launcher")
        return exe

    monkeypatch.setattr(sl, "ensure_launcher", fake_ensure)
    monkeypatch.setattr(
        backend, "_launch_direct", lambda: 0 if backend._direct_argv(game) else 1
    )
    _no_launcher_process(backend, monkeypatch)
    assert backend.launch() == 0
    assert seen == ["runner", "prefix", "launcher"]


def test_an_external_install_gets_its_prefix_without_a_pinned_runner(
    tmp_path, monkeypatch
):
    backend, game = _backend(
        tmp_path, {"install_dir": str(tmp_path / "library" / "Strinova")}
    )
    assert not (backend.prefix() / "drive_c").exists()
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda *, strict: None)
    # No Nadare runner is installed: the old code made this "no runtime", so no prefix.
    monkeypatch.setattr(runners, "installed_pinned", lambda family=None: None)
    monkeypatch.setattr(
        backend,
        "_prefix_command",
        lambda args, extra_env=None, *, sandboxed=False: (["/usr/bin/wine", *args], {}),
    )
    wineboots: list = []

    def fake_stream(argv, env, **kw):
        wineboots.append(argv)
        (backend.prefix() / "drive_c").mkdir(parents=True)
        return 0

    monkeypatch.setattr(winegame, "stream", fake_stream)
    exe = backend.prefix() / "drive_c" / "Strinova" / "Strinova.exe"
    seen: list = []

    def fake_ensure(prefix, game_dir, **kw):
        assert (backend.prefix() / "drive_c").is_dir()
        seen.append("launcher")
        return exe

    monkeypatch.setattr(sl, "ensure_launcher", fake_ensure)
    monkeypatch.setattr(
        backend, "_launch_direct", lambda: 0 if backend._direct_argv(game) else 1
    )
    _no_launcher_process(backend, monkeypatch)
    assert backend.launch() == 0
    assert wineboots and wineboots[0][-2:] == ["wineboot", "-u"]
    assert seen == ["launcher"]


def test_install_with_a_failed_runner_download_still_syncs(tmp_path, monkeypatch):
    # install() must stay as on b29: strict=False, then sync the files; it must not
    # create a prefix (and so must not reach wineboot) behind the runner fallback.
    backend, _ = _backend(
        tmp_path, {"install_dir": str(tmp_path / "library" / "Strinova")}
    )
    assert not (backend.prefix() / "drive_c").exists()
    monkeypatch.setattr(runners, "installed_pinned", lambda family=None: None)

    def offline(*_a, **_k):
        raise runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(runners, "ensure_pinned", offline)
    monkeypatch.setattr(
        backend, "_prefix_command", lambda *a, **k: pytest.fail("wineboot during install")
    )
    synced: list = []
    monkeypatch.setattr(backend, "_sync_direct", lambda: synced.append(True))
    backend.install()
    assert synced == [True]
    assert not (backend.prefix() / "drive_c").exists()


def test_a_launcher_failure_is_a_plain_backend_error(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path)

    def boom(*a, **k):
        raise sl.StrinovaLauncherError("7-Zip not found; install p7zip or 7zip")

    monkeypatch.setattr(sl, "ensure_launcher", boom)
    with pytest.raises(
        direct.BackendError,
        match="cannot prepare the Strinova launcher: 7-Zip not found",
    ):
        backend._direct_argv(game)


def test_the_installer_url_has_its_version_folder():
    # The publisher serves the installer under Launcher_Release/<version>/ (lastversion.json's
    # downloadUrl); the bare Launcher_Release/<file> path is a 404.
    m = re.search(
        r"/Launcher_Release/([0-9.]+)/Strinova_Installer_Release_([0-9.]+)\.exe$", sl.INSTALLER_URL
    )
    assert m and m.group(1) == m.group(2)
