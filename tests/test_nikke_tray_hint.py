# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE's page says when the launcher is still running with no window (closed to the tray).

Closing the launcher with its window button minimizes it to the tray; on a desktop without a
visible tray (e.g. Noctalia) it seemed to vanish while Nadare still showed it running
(user, 2026-11-10). The page tells, it never stops anything: auto-stop stays opt-in because the
launcher downloads updates inside its own process, which no busy check can see.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_mod
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import x11windows
from nadare.core.backends import Status


def _nikke(tmp_path, **profile):
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def running(monkeypatch):
    """A running NIKKE launcher: only tray-launcher processes, no window, up for 5 minutes."""
    state = {"shown": False, "busy": False, "age": 300.0, "display": "x11"}
    monkeypatch.setattr(WineGameBackend, "status", lambda self: Status("running", running=True))
    monkeypatch.setattr(x11windows, "any_shown", lambda titles, *a, **k: state["shown"])
    monkeypatch.setattr(NikkeBackend, "_nikke_busy_now", lambda self: state["busy"])
    monkeypatch.setattr(NikkeBackend, "_launcher_age", lambda self: state["age"])
    monkeypatch.setattr(NikkeBackend, "_display_in_use", lambda self: state["display"])
    return state


def test_a_launcher_with_no_window_says_it_is_in_the_background(tmp_path, running):
    status = _nikke(tmp_path).status()
    assert status.state == "running"
    assert status.data["launcher_hidden"] is True
    assert "background" in status.message and "Stop" in status.message


def test_no_hint_while_its_window_is_shown(tmp_path, running):
    running["shown"] = True
    status = _nikke(tmp_path).status()
    assert not status.data.get("launcher_hidden")
    assert "background" not in status.message


def test_no_hint_while_the_game_or_an_update_runs(tmp_path, running):
    running["busy"] = True
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")


def test_no_hint_when_windows_cannot_be_read(tmp_path, running):
    running["shown"] = None  # no X server reachable
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")


def test_no_hint_on_the_wayland_display_backend(tmp_path, running, monkeypatch):
    running["display"] = "wayland"
    asked = []
    monkeypatch.setattr(x11windows, "any_shown", lambda *a, **k: asked.append(1) or False)
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")
    assert asked == []  # X11 is not even asked


def test_no_hint_for_a_launcher_that_just_started(tmp_path, running):
    running["age"] = 5.0  # its window has not opened yet
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")


def test_no_hint_when_the_launcher_age_is_unknown(tmp_path, running):
    running["age"] = None
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")


def test_no_check_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(WineGameBackend, "status", lambda self: Status("installed"))
    monkeypatch.setattr(
        x11windows, "any_shown", lambda *a, **k: pytest.fail("windows read while idle")
    )
    assert not _nikke(tmp_path).status().data.get("launcher_hidden")


def test_a_failing_window_check_never_breaks_status(tmp_path, running, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("X went away")

    monkeypatch.setattr(x11windows, "any_shown", boom)
    status = _nikke(tmp_path).status()
    assert status.state == "running"
    assert not status.data.get("launcher_hidden")


def test_the_launcher_age_comes_from_the_launcher_process(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(launcher_mod, "scan_pids", lambda query: [4242])
    monkeypatch.setattr(
        launcher_mod, "process_age_seconds", lambda pid: 120.0 if pid == 4242 else None
    )
    assert backend._launcher_age() == 120.0
    monkeypatch.setattr(launcher_mod, "scan_pids", lambda query: [])
    assert backend._launcher_age() is None


def test_process_age_seconds_reads_proc(tmp_path):
    import os
    import time

    from nadare.core import process

    age = process.process_age_seconds(os.getpid())
    assert age is not None and 0 <= age < time.monotonic() + 60
    assert process.process_age_seconds(2**22 + 12345) is None  # no such process


def test_the_hint_works_on_a_plain_x11_session(tmp_path, running):
    running["display"] = "x11 (no Wayland session)"  # what _display_in_use says without Wayland
    assert _nikke(tmp_path).status().data["launcher_hidden"] is True


def test_auto_stop_also_works_on_a_plain_x11_session(tmp_path, monkeypatch):
    monkeypatch.setattr(NikkeBackend, "_display_in_use", lambda self: "x11 (no Wayland session)")
    backend = _nikke(tmp_path, stop_when_hidden=True)
    assert backend._hidden_watch() is not None
