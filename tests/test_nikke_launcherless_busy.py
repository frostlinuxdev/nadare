# SPDX-License-Identifier: GPL-3.0-or-later
"""A launcherless Play refuses to start next to NIKKE's own launcher (#1388).

A tester's game exited about 20 s after the sign-in handshake while the launcher was still
running (maybe closed to the tray). Its session and Sail's cannot share the prefix, so the
launcherless path says to close the launcher first instead of starting the game beside it.
Nothing here runs Wine or reads the real ``/proc``.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendError


def _nikke(tmp_path, *, launcherless: bool = True) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    launcher = write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    write_exe(prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe", "MZ")
    (prefix / "drive_c" / "NIKKE" / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    storage = h.cache_storage_dir(prefix)
    write_exe(storage / "intl_auth.txt", "session")
    write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": launcherless}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def ran(monkeypatch):
    """Record which launch path ran (no Wine, no runner download)."""
    calls: list[str] = []
    monkeypatch.setattr(NikkeBackend, "_update_before_play", lambda self: calls.append("update"))
    monkeypatch.setattr(
        NikkeBackend, "_launch_launcherless", lambda self, files: calls.append("game") or 0
    )
    monkeypatch.setattr(NikkeBackend, "_ensure_default_runner", lambda self, *, strict: None)
    monkeypatch.setattr(
        NikkeBackend, "_launch_with_watch", lambda self: calls.append("launcher") or 0
    )
    return calls


def test_a_running_launcher_stops_a_launcherless_play(tmp_path, ran, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(NikkeBackend, "_launcher_pids", lambda self: [4242])

    with pytest.raises(BackendError, match="launcher is still running"):
        backend.launch()

    # Nothing launcherless happened: no update and no session restore.
    assert ran == []


def test_no_launcher_means_the_launcherless_path_runs_as_before(tmp_path, ran, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(NikkeBackend, "_launcher_pids", lambda self: [])

    assert backend.launch() == 0
    assert ran == ["update", "game"]


def test_with_the_option_off_the_launcher_opens_and_nothing_is_refused(tmp_path, ran, monkeypatch):
    backend = _nikke(tmp_path, launcherless=False)
    monkeypatch.setattr(NikkeBackend, "_launcher_pids", lambda self: [4242])

    assert backend.launch() == 0
    assert ran == ["launcher"]


def test_only_this_profiles_prefix_is_scanned(tmp_path, monkeypatch):
    """A launcher in another profile's prefix must not block this one."""
    backend = _nikke(tmp_path)
    seen = []
    monkeypatch.setattr(launcher_module, "scan_pids", lambda query: seen.append(query) or [])

    assert backend._launcher_pids() == []
    assert [query.prefix for query in seen] == [str(backend.prefix())]


def test_the_game_and_the_shared_service_are_not_the_launcher(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(
        NikkeBackend,
        "_prefix_processes",
        lambda self: [
            (11, r"C:\NIKKE\NIKKE\game\nikke.exe"),
            (12, r"C:\NIKKE\Launcher\nikke_launcher.exe"),
            (13, r"C:\NIKKE\Launcher\tbs_browser.exe"),
            # a launcherless run starts this one too: alone it is not the launcher
            (14, r"C:\NIKKE\Launcher\intl_service\intl_service.exe"),
        ],
    )

    assert backend._launcher_pids() == [12, 13]
