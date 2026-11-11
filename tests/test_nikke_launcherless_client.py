# SPDX-License-Identifier: GPL-3.0-or-later
"""A launcherless Play on a launcher-only install downloads the game client itself.

Found by the maintainer on 2026-11-11: NIKKE installed by b22 has the launcher but no game client
(b22 left the client to the launcher). With "Start the game without the NIKKE launcher" on and a
saved sign-in, b23 opened the launcher anyway ("the game is not installed yet; the launcher will
download it"). Nadare already downloads the client itself at install time, so Play does the same:
download the client, then start the game without the launcher.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendError


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    # The version check before Play is not what is tested here: the client is current.
    monkeypatch.setattr(NikkeBackend, "_fetch_client_latest", lambda self: None)
    return tmp_path


def _launcher_only(tmp_path, *, signed_in=True, launcherless=True) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    launcher = write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    if signed_in:
        storage = h.cache_storage_dir(prefix)
        write_exe(storage / "intl_auth.txt", "session")
        write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": launcherless}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def downloads(monkeypatch):
    calls = []

    def install_game(dest, *, emit=None, cancel=None, post=None, fetch=None):
        calls.append(dest)
        exe = dest / "NIKKE" / "game" / "nikke.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ client")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", install_game)
    return calls


@pytest.fixture
def runs(monkeypatch):
    ran = []
    monkeypatch.setattr(
        NikkeBackend, "_launch_launcherless", lambda self, files: ran.append("game") or 0
    )
    monkeypatch.setattr(
        NikkeBackend, "_launch_with_watch", lambda self: ran.append("launcher") or 0
    )
    monkeypatch.setattr(h, "stage_hosts", lambda prefix: None)
    return ran


def test_play_downloads_the_missing_client_then_starts_without_the_launcher(env, downloads, runs):
    backend = _launcher_only(env)
    assert backend.launch() == 0
    assert downloads == [backend._launcher_root()]
    assert runs == ["game"]
    assert not backend._update_marker().exists()


def test_play_says_it_is_downloading_the_client(env, downloads, runs):
    backend = _launcher_only(env)
    steps = []
    backend.ctx.emit = lambda event: steps.append(event.message)
    backend.launch()
    assert any("game client" in step for step in steps)


def test_without_a_sign_in_the_launcher_opens_and_nothing_is_downloaded(env, downloads, runs):
    backend = _launcher_only(env, signed_in=False)
    assert backend.launch() == 0
    assert downloads == []
    assert runs == ["launcher"]


def test_with_the_option_off_the_launcher_downloads_the_game(env, downloads, runs):
    backend = _launcher_only(env, launcherless=False)
    assert backend.launch() == 0
    assert downloads == []
    assert runs == ["launcher"]


def test_a_client_on_another_drive_is_not_downloaded_again(env, downloads, runs, tmp_path):
    backend = _launcher_only(env)
    other = tmp_path / "drive_d"
    write_exe(other / "NIKKE" / "game" / "nikke.exe", "MZ")
    (backend.prefix() / "dosdevices").mkdir(parents=True, exist_ok=True)
    (backend.prefix() / "dosdevices" / "d:").symlink_to(other)
    assert backend.launch() == 0
    assert downloads == []
    assert runs == ["game"]


def test_a_failed_client_download_says_what_to_do_and_starts_nothing(env, runs, monkeypatch):
    def broken(dest, **_kw):
        raise launcher_module.railrepo.RailError("connection reset")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", broken)
    backend = _launcher_only(env)
    with pytest.raises(BackendError, match="launcher"):
        backend.launch()
    assert runs == []
    # the next Play tries again (the client is still incomplete)
    assert backend._update_marker().exists()


def test_no_client_download_while_the_launcher_runs(env, downloads, runs, monkeypatch):
    monkeypatch.setattr(launcher_module, "scan_pids", lambda query: [4242])
    backend = _launcher_only(env)
    backend.launch()
    assert downloads == []
    assert runs == ["launcher"]


def test_falling_back_to_the_launcher_says_why_where_the_user_sees_it(env, downloads, runs):
    """The reason was only in the collapsed log; the maintainer saw the launcher and no why."""
    backend = _launcher_only(env, signed_in=False)
    events = []
    backend.ctx.emit = events.append
    backend.launch()
    steps = [e.message for e in events if getattr(e, "kind", None) == "step"]
    assert any("launcher" in s and "sign-in" in s for s in steps), steps


def test_no_fallback_note_when_the_option_is_off(env, downloads, runs):
    backend = _launcher_only(env, launcherless=False)
    events = []
    backend.ctx.emit = events.append
    backend.launch()
    assert not any(getattr(e, "kind", None) == "step" and "without" in e.message for e in events)
