# SPDX-License-Identifier: GPL-3.0-or-later
"""#1338 (user decision 2026-11-14): every Proton launch goes through umu, outside the Flatpak
Sandbox, for every user.

A tester with the 32-bit libraries and no system umu ran ``proton run``: Proton skipped every
per-game fix (NIKKE's ``disable_nvapi``: no videos on a GTX 1650) and no Steam Runtime was
used. Now: a system umu (new enough) -> the managed copy (installed from the AppImage's own
bundled archive, no network) -> a download; never ``proton run`` on a desktop install.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from test_backends_arknights import _backend, _install_launcher

from nadare.backends import winegame as wg
from nadare.core import umu, userns
from nadare.core.backends import BackendError


def _proton(tmp_path, monkeypatch, *, flatpak=False):
    prefix = tmp_path / "prefix"
    _install_launcher(prefix)
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: flatpak)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: [])
    monkeypatch.setattr(userns, "restricted", lambda: False)
    return _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"}), proton


# ------------------------------------------------------------------ the launch rule
def test_no_system_umu_with_32bit_libs_uses_the_managed_umu(tmp_path, monkeypatch):
    """The tester's machine: 32-bit libraries present, no system umu-run."""
    backend, proton = _proton(tmp_path, monkeypatch)
    asked: list[bool] = []

    def resolve(configured="umu-run", *, allow_download, emit=None, cancel=None):
        asked.append(allow_download)
        return "/data/nadare/umu/1.4.4/umu-run"

    monkeypatch.setattr(umu, "resolve", resolve)
    argv, env, _target = backend.build_launch()
    assert argv[0] == "/data/nadare/umu/1.4.4/umu-run"
    assert "run" not in argv[:2] and str(proton / "proton") not in argv
    assert asked == [True]
    assert env["PROTONPATH"] == str(proton)


def test_no_umu_at_all_refuses_instead_of_proton_run(tmp_path, monkeypatch):
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: None)
    with pytest.raises(BackendError, match="umu"):
        backend.build_launch()


def test_a_system_umu_still_wins(tmp_path, monkeypatch):
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(
        umu, "resolve", lambda configured="umu-run", **k: "/usr/bin/umu-run"
    )
    argv, _env, _target = backend.build_launch()
    assert argv[0] == "/usr/bin/umu-run"


def test_the_flatpak_sandbox_keeps_its_direct_proton(tmp_path, monkeypatch):
    """Separate issue: umu's container needs the session bus a sandboxed game must not have."""
    backend, proton = _proton(tmp_path, monkeypatch, flatpak=True)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: pytest.fail("no umu in the Sandbox"))
    argv, _env = backend._prefix_command(["x.exe"], sandboxed=True)
    assert argv[:2] == [str(proton / "proton"), "run"]


# ------------------------------------------------------------------ Ubuntu AppArmor first
def test_ubuntu_blocked_userns_asks_before_the_launch(tmp_path, monkeypatch):
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: False)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: False)
    with pytest.raises(BackendError) as caught:
        backend.build_launch()
    assert userns.has_hint(str(caught.value))  # the GUI opens the one-time fix dialog


def test_ubuntu_with_the_profile_installed_launches(tmp_path, monkeypatch):
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: True)
    argv, _env, _target = backend.build_launch()
    assert argv[0] == "/usr/bin/umu-run"


def test_ubuntu_where_the_probe_works_launches(tmp_path, monkeypatch):
    """Restricted on paper but bwrap works (an unconfined setup): do not block."""
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: False)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: True)
    argv, _env, _target = backend.build_launch()
    assert argv[0] == "/usr/bin/umu-run"


# ------------------------------------------------------------------ the bundled archive
def _fake_tar(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("umu/umu-run")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture
def _managed_home(tmp_path, monkeypatch):
    monkeypatch.setattr(umu, "managed_path", lambda: tmp_path / "data" / "umu" / "umu-run")
    monkeypatch.setattr(umu.paths, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(umu.os.path, "isfile", lambda p: True if p == umu.SHEBANG_PYTHON
                        else Path(p).is_file())
    monkeypatch.setattr(umu.shutil, "which", lambda name, *_a, **_k: "/usr/bin/python3")
    monkeypatch.setattr(umu, "_smoke_test", lambda python, executable: None)
    return tmp_path


def test_the_bundled_archive_installs_without_the_network(_managed_home, monkeypatch):
    data = _fake_tar(b"#!/usr/bin/python3\nprint('umu')\n")
    bundle = _managed_home / "assets" / f"umu-launcher-{umu.UMU_VERSION}-zipapp.tar"
    bundle.parent.mkdir()
    bundle.write_bytes(data)
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", bundle)
    monkeypatch.setattr(umu, "UMU_TAR_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(umu.download, "download", lambda *a, **k: pytest.fail("downloaded"))
    path = umu.ensure_managed()
    assert path == umu.managed_path() and path.read_bytes().startswith(b"#!/usr/bin/python3")
    assert bundle.is_file()  # the bundle itself is never consumed


def test_a_bundled_archive_with_the_wrong_hash_is_never_installed(_managed_home, monkeypatch):
    bundle = _managed_home / "assets" / "umu.tar"
    bundle.parent.mkdir()
    bundle.write_bytes(_fake_tar(b"evil"))
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", bundle)
    monkeypatch.setattr(umu, "UMU_TAR_SHA256", "0" * 64)

    def no_network(*a, **k):
        raise umu.download.DownloadError("offline")

    monkeypatch.setattr(umu.download, "download", no_network)
    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert not umu.managed_path().exists()


def test_without_a_bundle_it_still_downloads(_managed_home, monkeypatch):
    data = _fake_tar(b"#!/usr/bin/python3\n")
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", _managed_home / "absent.tar")
    monkeypatch.setattr(umu, "UMU_TAR_SHA256", hashlib.sha256(data).hexdigest())
    got: list[str] = []

    def fake_download(url, dest, **kwargs):
        got.append(url)
        Path(dest).write_bytes(data)
        from types import SimpleNamespace

        return SimpleNamespace(path=Path(dest))

    monkeypatch.setattr(umu.download, "download", fake_download)
    assert umu.ensure_managed().is_file() and got


def test_the_appimage_build_fetches_the_pinned_umu():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts" / "build_appimage.sh").read_text()
    fetch = (root / "scripts" / "fetch_umu.sh").read_text()
    assert "bash scripts/fetch_umu.sh" in build
    assert umu.UMU_TAR_SHA256 in fetch and umu.UMU_VERSION in fetch
    assert "src/nadare/assets/umu-launcher-" in (root / ".gitignore").read_text()


# ------------------------------------------------------------------ the generic Wine/Proton game
@pytest.fixture
def _generic(tmp_path, monkeypatch):
    from _backend_helpers import make_context, write_exe

    from nadare.backends import wine as wine_mod
    from nadare.core import runners

    pinned = tmp_path / "runners" / "dwproton"
    pinned.mkdir(parents=True)
    (pinned / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runners, "installed_pinned", lambda family=runners.DEFAULT_FAMILY: pinned)
    monkeypatch.setattr(runners, "ensure_pinned", lambda family=None, **kw: pinned)
    monkeypatch.setattr(umu, "system", lambda configured=None, **k: None)
    monkeypatch.setattr(umu, "managed", lambda: None)
    monkeypatch.setattr(wine_mod.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    ctx = make_context(tmp_path, "wine", {"install_dir": str(game), "exe_name": "game.exe",
                                          "prefix": str(tmp_path / "pfx")})
    return wine_mod.WineBackend(ctx)


def test_generic_proton_game_without_system_umu_uses_the_managed_one(_generic, monkeypatch):
    asked: list[bool] = []
    monkeypatch.setattr(umu, "resolve", lambda configured="umu-run", *, allow_download, **k:
                        asked.append(allow_download) or "/data/umu/umu-run")
    argv, *_ = _generic.build()
    assert argv[0] == "/data/umu/umu-run" and asked == [True]


def test_generic_proton_game_with_no_umu_explains(_generic, monkeypatch):
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: None)
    with pytest.raises(BackendError, match="umu"):
        _generic.build()


def test_generic_status_does_not_call_a_missing_umu_an_error(_generic, monkeypatch):
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: pytest.fail("status must not set up"))
    assert _generic._runtime_error() is None  # launch installs the bundled copy


def test_generic_ubuntu_blocked_asks_before_the_launch(_generic, monkeypatch):
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: False)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: None)
    with pytest.raises(BackendError) as caught:
        _generic.build()
    assert userns.has_hint(str(caught.value))
