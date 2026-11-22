# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE stop/start, tightened (b24 reports: the page still said running).

The launcher's own default, Minimize, keeps it in the tray, so Nadare kept waiting and showing
STOP. The tray auto-stop was opt-in (its risk: stopping a launcher that downloads an update in
its own process). Now:

- the auto-stop is on by default, and a launcher that writes to disk (an update) counts as
  busy, so it is never stopped then;
- status and Stop always know the game (``nikke.exe``, the sign-in host), not only inside the
  Nadare instance that started a launcherless run;
- Play with the launcher idle in the tray stops it and starts cleanly, instead of refusing
  (launcherless, #1388) or starting a second launcher next to it. A busy or still-starting
  launcher is left alone and Play says why.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_mod
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core import trayidle
from nadare.core.backends import BackendError, get


def _nikke(tmp_path, **profile) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    launcher = write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    write_exe(prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe", "MZ")
    (prefix / "drive_c" / "NIKKE" / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    storage = h.cache_storage_dir(prefix)
    write_exe(storage / "intl_auth.txt", "session")
    write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", **profile}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


# --- default on ----------------------------------------------------------------------------


def test_auto_stop_is_on_by_default():
    option = next(o for o in get("launcher-nikke").options if o.key == "stop_when_hidden")
    assert option.default is True


def test_the_watch_runs_without_the_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(NikkeBackend, "_display_in_use", lambda self: "x11")
    assert _nikke(tmp_path)._hidden_watch() is not None


def test_turning_it_off_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(NikkeBackend, "_display_in_use", lambda self: "x11")
    assert _nikke(tmp_path, stop_when_hidden=False)._hidden_watch() is None


# --- an updating launcher is busy ----------------------------------------------------------


def test_disk_writes_count_as_busy():
    written = {1: 0}
    activity = trayidle.WriteActivity(reader=lambda pid: written.get(pid))
    assert activity.active([1]) is False  # the first look is the baseline
    written[1] += 64 * 1024**2
    assert activity.active([1]) is True
    assert activity.active([1]) is False  # nothing new written since


def test_small_writes_are_not_an_update():
    written = {1: 0}
    activity = trayidle.WriteActivity(reader=lambda pid: written.get(pid))
    activity.active([1])
    written[1] += 200 * 1024  # logs, cookies
    assert activity.active([1]) is False


def test_unreadable_io_is_ignored_not_busy():
    activity = trayidle.WriteActivity(reader=lambda pid: None)
    assert activity.active([1]) is False
    assert activity.active([1]) is False


def test_a_new_process_starts_at_its_own_baseline():
    written = {1: 0, 2: 900 * 1024**2}
    activity = trayidle.WriteActivity(reader=lambda pid: written.get(pid))
    activity.active([1])
    assert activity.active([1, 2]) is False  # pid 2's history is not "new writes"


def test_the_backend_counts_a_writing_launcher_as_busy(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(NikkeBackend, "_prefix_processes",
                        lambda self: [(10, "C:\\NIKKE\\Launcher\\nikke_launcher.exe")])
    written = {10: 0}
    backend._writes = trayidle.WriteActivity(reader=lambda pid: written.get(pid))
    assert backend._nikke_busy_now() is False
    written[10] += 100 * 1024**2
    assert backend._nikke_busy_now() is True


# --- status and Stop know the game ---------------------------------------------------------


def test_status_patterns_always_include_the_game(tmp_path):
    names = _nikke(tmp_path)._patterns().names
    assert "nikke.exe" in names and h.SAIL_HOST_NAME in names


# --- Play next to a tray launcher -----------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(NikkeBackend, "_update_before_play", lambda self: calls.append("update"))
    monkeypatch.setattr(NikkeBackend, "_launch_launcherless",
                        lambda self, files: calls.append("game") or 0)
    monkeypatch.setattr(NikkeBackend, "_ensure_default_runner", lambda self, *, strict: None)
    monkeypatch.setattr(NikkeBackend, "_launch_with_watch",
                        lambda self: calls.append("launcher") or 0)
    return calls


def _tray(monkeypatch, *, shown, busy=False, age=300.0):
    pids = [4242]
    stopped: list[list[int]] = []
    monkeypatch.setattr(NikkeBackend, "_launcher_pids", lambda self: list(pids))
    monkeypatch.setattr(NikkeBackend, "_display_in_use", lambda self: "x11")
    monkeypatch.setattr(launcher_mod.x11windows, "any_shown", lambda titles: shown)
    monkeypatch.setattr(launcher_mod.x11windows, "any_window_open",
                        lambda: False if shown is False else shown)
    monkeypatch.setattr(NikkeBackend, "_nikke_busy_sampled", lambda self: busy)
    monkeypatch.setattr(NikkeBackend, "_launcher_age", lambda self: age)

    def stop(self, p):
        stopped.append(list(p))
        pids.clear()

    monkeypatch.setattr(NikkeBackend, "_stop_game_processes", stop)
    return stopped


def test_launcherless_play_stops_an_idle_tray_launcher_first(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=False)
    monkeypatch.setattr(NikkeBackend, "_launcherless_plan", lambda self: ["files"])
    assert _nikke(tmp_path, launcherless=True).launch() == 0
    assert stopped == [[4242]] and ran[-1] == "game"


def test_launcher_play_restarts_an_idle_tray_launcher(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=False)
    assert _nikke(tmp_path).launch() == 0
    assert stopped == [[4242]] and ran == ["launcher"]


def test_a_shown_launcher_is_not_stopped(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=True)
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert stopped == []


def test_a_busy_launcher_is_not_stopped(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=False, busy=True)
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert stopped == []


def test_a_launcher_still_starting_is_not_stopped(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=False, age=5.0)
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert stopped == []


def test_unknown_window_state_is_not_stopped(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=None)
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert stopped == []


def test_a_launcher_that_will_not_stop_refuses_play(tmp_path, ran, monkeypatch):
    _tray(monkeypatch, shown=False)
    monkeypatch.setattr(NikkeBackend, "_stop_game_processes", lambda self, p: None)  # ignores
    monkeypatch.setattr(launcher_mod, "_LAUNCHER_STOP_WAIT", 0.1)
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert "game" not in ran


def test_launcher_play_beside_a_shown_launcher_is_unchanged(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=True)
    assert _nikke(tmp_path).launch() == 0
    assert stopped == [] and ran == ["launcher"]


def test_play_waits_for_a_launcher_that_takes_a_moment_to_exit(tmp_path, ran, monkeypatch):
    _tray(monkeypatch, shown=False)
    left = {"polls": 3}

    def pids(self):
        if left["polls"] > 0 and stopped_once:
            left["polls"] -= 1
        return [4242] if left["polls"] > 0 else []

    stopped_once: list[bool] = []
    monkeypatch.setattr(NikkeBackend, "_launcher_pids", pids)
    monkeypatch.setattr(NikkeBackend, "_stop_game_processes",
                        lambda self, p: stopped_once.append(True))
    monkeypatch.setattr(launcher_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(NikkeBackend, "_launcherless_plan", lambda self: ["files"])
    assert _nikke(tmp_path, launcherless=True).launch() == 0
    assert stopped_once and ran[-1] == "game"


# --- any open launcher window, whatever its title (rig 2026-11-15: an untitled "System
# Update" prompt) ------------------------------------------------------------------------


from nadare.core import x11windows  # noqa: E402


def test_an_untitled_dialog_counts_as_open():
    windows = [
        ("steam_proton", "", True, (480, 300)),
        ("steam_proton", "NIKKE", False, (1912, 986)),
    ]
    assert x11windows.window_open_from(windows) is True


def test_tiny_helper_windows_do_not_count():
    windows = [("steam_proton", "Default IME", True, (1, 1)), ("steam_proton", "", True, (113, 2))]
    assert x11windows.window_open_from(windows) is False


def test_other_programs_do_not_count():
    assert x11windows.window_open_from([("firefox", "", True, (800, 600))]) is False


def test_no_display_means_unknown_for_open(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert x11windows.any_window_open() is None


def test_play_leaves_a_launcher_with_an_untitled_prompt_alone(tmp_path, ran, monkeypatch):
    stopped = _tray(monkeypatch, shown=False)  # no window titled NIKKE ...
    monkeypatch.setattr(launcher_mod.x11windows, "any_window_open", lambda: True)  # ... a prompt
    with pytest.raises(BackendError, match="launcher is still running"):
        _nikke(tmp_path, launcherless=True).launch()
    assert stopped == []


def test_the_hidden_main_window_in_the_tray_does_not_count():
    windows = [
        ("steam_proton", "NIKKE", False, (1912, 986)),
        ("steam_proton", "", False, (480, 300)),
    ]
    assert x11windows.window_open_from(windows) is False
