# SPDX-License-Identifier: GPL-3.0-or-later
"""Stop NIKKE's launcher once its window is closed to the tray (opt-in).

Measured on a KDE Wayland desktop with NIKKE on X11 (XWayland), Flatpak + Sandbox, 2026-11-10:
the launcher's visible window is an X11 window titled ``NIKKE`` with class ``steam_proton``;
a second, always unmapped window is titled ``nikke_launcher``. KWin under Wayland keeps
``_NET_CLIENT_LIST`` empty, so the window tree itself is read. Signed out, closing the window
quits the launcher; signed in it hides in the tray (the case this watch is for). Under native
Wayland no X11 window exists: the check answers "unknown" and nothing is ever stopped.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_mod
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import trayidle, x11windows
from nadare.core.backends import get
from nadare.core.events import Cancelled

# --- the X11 check -------------------------------------------------------------------------


def test_a_shown_launcher_window_counts():
    windows = [
        ("steam_proton", "nikke_launcher", False),
        ("steam_proton", "NIKKE", True),
    ]
    assert x11windows.shown_from(windows, ("NIKKE",)) is True


def test_an_unmapped_launcher_window_is_not_shown():
    windows = [
        ("steam_proton", "nikke_launcher", False),
        ("steam_proton", "NIKKE", False),
        ("steam_proton", "Default IME", True),
    ]
    assert x11windows.shown_from(windows, ("NIKKE",)) is False


def test_a_window_of_another_program_with_the_same_title_does_not_count():
    windows = [("firefox", "NIKKE", True)]
    assert x11windows.shown_from(windows, ("NIKKE",)) is False


def test_the_title_must_match_exactly():
    windows = [("steam_proton", "NIKKE - Event", True), ("steam_proton", "nikke", True)]
    assert x11windows.shown_from(windows, ("NIKKE",)) is False


@pytest.mark.parametrize(
    ("map_state", "wm_state", "shown"),
    [
        (2, 1, True),  # IsViewable, NormalState
        (0, 3, True),  # minimized (measured): IsUnmapped + IconicState, still on the desktop
        (0, None, False),  # in the tray (measured): unmapped, no WM_STATE
        (0, 0, False),  # WithdrawnState
        (2, None, True),
    ],
)
def test_a_minimized_window_still_counts_as_shown(map_state, wm_state, shown):
    assert x11windows.is_shown(map_state, wm_state) is shown


def test_no_display_means_unknown(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert x11windows.any_shown(("NIKKE",)) is None


def test_no_libx11_means_unknown(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(x11windows, "list_windows", lambda: None)
    assert x11windows.any_shown(("NIKKE",)) is None


def test_a_failing_window_list_means_unknown(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")

    def boom():
        raise OSError("X server went away")

    monkeypatch.setattr(x11windows, "list_windows", boom)
    assert x11windows.any_shown(("NIKKE",)) is None


def test_list_windows_without_a_display_returns_none(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":nadare-no-such-display")
    assert x11windows.list_windows() is None


# --- the idle timer ------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _watch(shown, busy=lambda: False, idle=60.0):
    clock = _Clock()
    fired = []
    watch = trayidle.HiddenWindowWatch(
        shown, busy, lambda: fired.append(clock.now), idle_seconds=idle, clock=clock
    )
    return watch, clock, fired


def test_never_stops_before_the_window_was_seen():
    watch, clock, fired = _watch(lambda: False)
    for _ in range(100):
        clock.now += 10
        watch.step()
    assert fired == [] and not watch.fired


def test_stops_once_after_the_window_is_hidden_long_enough():
    states = iter([True, False, False, False, False, False, False, False, False])
    watch, clock, fired = _watch(lambda: next(states, False))
    watch.step()  # seen
    for _ in range(8):
        clock.now += 10
        watch.step()
    assert len(fired) == 1 and watch.fired


def test_not_before_the_idle_time():
    states = iter([True] + [False] * 6)
    watch, clock, fired = _watch(lambda: next(states, False), idle=60.0)
    watch.step()
    clock.now += 1
    watch.step()  # hidden from here (t0)
    for _ in range(5):
        clock.now += 11  # 55 s hidden
        watch.step()
    assert fired == []


def test_the_window_coming_back_resets_the_timer():
    seq = [True, False, False, False, True, False, False, False]
    states = iter(seq)
    watch, clock, fired = _watch(lambda: next(states, False), idle=60.0)
    for _ in seq:
        watch.step()
        clock.now += 20
    assert fired == []


def test_unknown_resets_the_timer_and_never_stops():
    seq = [True, False, False, None, False, False]
    states = iter(seq)
    watch, clock, fired = _watch(lambda: next(states, None), idle=60.0)
    for _ in seq:
        watch.step()
        clock.now += 25
    assert fired == []
    for _ in range(20):
        watch.step()
        clock.now += 25
    assert fired == []


def test_a_failing_window_check_never_stops():
    calls = {"n": 0}

    def shown():
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        raise RuntimeError("broken")

    watch, clock, fired = _watch(shown)
    for _ in range(20):
        watch.step()
        clock.now += 30
    assert fired == []


def test_a_busy_prefix_holds_the_stop():
    busy = {"v": True}
    states = iter([True] + [False] * 30)
    watch, clock, fired = _watch(lambda: next(states, False), busy=lambda: busy["v"])
    for _ in range(10):
        watch.step()
        clock.now += 30
    assert fired == []
    busy["v"] = False
    for _ in range(2):
        watch.step()
        clock.now += 30
    assert fired == []  # the hidden time starts again once nothing else runs
    for _ in range(2):
        watch.step()
        clock.now += 30
    assert len(fired) == 1


def test_a_failing_busy_check_counts_as_busy():
    def busy():
        raise OSError("proc gone")

    states = iter([True] + [False] * 30)
    watch, clock, fired = _watch(lambda: next(states, False), busy=busy)
    for _ in range(20):
        watch.step()
        clock.now += 30
    assert fired == []


def test_the_thread_stops_cleanly():
    watch = trayidle.HiddenWindowWatch(
        lambda: None, lambda: False, lambda: None, idle_seconds=60.0, poll_seconds=0.01
    )
    watch.start()
    watch.stop()
    assert not watch.fired


# --- what counts as "something else is running" -------------------------------------------


def test_the_tray_launcher_alone_is_not_busy():
    # argv[0] of every process in the prefix with the window in the tray (measured).
    argv0 = [
        "C:\\windows\\system32\\csrss.exe", "C:\\windows\\system32\\explorer.exe",
        "/app/bin/flatpak-spawn", "--LauncherPID=328", "C:\\windows\\system32\\lsass.exe",
        "C:\\NIKKE\\Launcher\\nikke_launcher.exe", "C:\\windows\\system32\\plugplay.exe",
        "--ProductName=nikke_launcher", "python3", "C:\\windows\\system32\\rpcss.exe",
        "C:\\windows\\system32\\services.exe", "sh", "C:\\windows\\system32\\svchost.exe",
        "C:\\windows\\system32\\tabtip.exe",
        "C:\\NIKKE\\Launcher\\intl_service\\tbs_browser.exe", "umu.exe",
        "C:\\windows\\system32\\winedevice.exe", "wineserver", "C:\\windows\\system32\\xalia.exe",
    ]
    assert launcher_mod.nikke_busy(argv0) is False


@pytest.mark.parametrize(
    "exe",
    [
        "C:\\NIKKE\\Game\\nikke.exe",
        "C:\\NIKKE\\Launcher\\VersionService.exe",
        "/home/x/Games/nikke/prefix/drive_c/NIKKE/Game/NIKKE.exe",
        "C:\\NIKKE\\Game\\UnityCrashHandler64.exe",
    ],
)
def test_the_game_or_an_updater_makes_it_busy(exe):
    assert launcher_mod.nikke_busy(["C:\\NIKKE\\Launcher\\nikke_launcher.exe", exe]) is True


# --- the NIKKE backend ---------------------------------------------------------------------


def _nikke(tmp_path, **profile):
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def test_the_option_exists_and_is_off_by_default():
    option = next(o for o in get("launcher-nikke").options if o.key == "stop_when_hidden")
    assert option.kind == "bool" and option.default is False
    assert "tray" in option.help


def test_other_launcher_games_do_not_get_the_option():
    from _backend_helpers import fake_launcher_backend

    assert all(o.key != "stop_when_hidden" for o in fake_launcher_backend().options)


class _FakeWatch:
    made: list[_FakeWatch] = []

    def __init__(self, shown, busy, on_idle, **kwargs):
        self.shown, self.busy, self.on_idle = shown, busy, on_idle
        self.fired = False
        self.started = self.stopped = False
        _FakeWatch.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_watch(monkeypatch):
    _FakeWatch.made = []
    monkeypatch.setattr(launcher_mod, "HiddenWindowWatch", _FakeWatch)
    return _FakeWatch


def test_off_by_default_no_watch(tmp_path, monkeypatch, fake_watch):
    monkeypatch.setattr(WineGameBackend, "launch", lambda self: 0)
    assert _nikke(tmp_path).launch() == 0
    assert fake_watch.made == []


def test_no_watch_under_native_wayland(tmp_path, monkeypatch, fake_watch):
    monkeypatch.setattr(WineGameBackend, "launch", lambda self: 0)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")  # a real Wayland session
    assert _nikke(tmp_path, stop_when_hidden=True, display="wayland").launch() == 0
    assert fake_watch.made == []


def test_the_watch_runs_for_the_launch_only(tmp_path, monkeypatch, fake_watch):
    seen = {}

    def launch(self):
        seen["started"] = fake_watch.made[0].started
        return 0

    monkeypatch.setattr(WineGameBackend, "launch", launch)
    assert _nikke(tmp_path, stop_when_hidden=True).launch() == 0
    assert seen["started"] and fake_watch.made[0].stopped


def test_the_watch_looks_for_the_nikke_window(tmp_path, monkeypatch, fake_watch):
    asked = []
    monkeypatch.setattr(
        launcher_mod.x11windows, "any_shown", lambda titles: asked.append(tuple(titles)) or True
    )
    monkeypatch.setattr(WineGameBackend, "launch", lambda self: 0)
    _nikke(tmp_path, stop_when_hidden=True).launch()
    assert fake_watch.made[0].shown() is True
    assert asked == [("NIKKE",)]


def test_an_auto_stop_ends_the_launch_as_a_normal_stop(tmp_path, monkeypatch, fake_watch):
    stopped = []
    backend = _nikke(tmp_path, stop_when_hidden=True)
    monkeypatch.setattr(launcher_mod, "scan_pids", lambda query: [4242])
    monkeypatch.setattr(type(backend), "_stop_game_processes", lambda self, p: stopped.append(p))

    def launch(self):
        watch = fake_watch.made[0]
        watch.on_idle()
        watch.fired = True
        self.ctx.cancel.raise_if_cancelled()
        return 0

    monkeypatch.setattr(WineGameBackend, "launch", launch)
    assert backend.launch() == 0
    assert stopped == [[4242]]
    assert fake_watch.made[0].stopped


def test_a_user_cancel_is_still_a_cancel(tmp_path, monkeypatch, fake_watch):
    backend = _nikke(tmp_path, stop_when_hidden=True)

    def launch(self):
        self.ctx.cancel.cancel()
        self.ctx.cancel.raise_if_cancelled()
        return 0

    monkeypatch.setattr(WineGameBackend, "launch", launch)
    with pytest.raises(Cancelled):
        backend.launch()
    assert fake_watch.made[0].stopped
