# SPDX-License-Identifier: GPL-3.0-or-later
"""Window evidence must belong to this launch, including launcher descendants."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from nadare.backends import wine
from nadare.core import launch_window, x11windows
from nadare.core.config import Config
from nadare.core.events import Cancel
from nadare.gui import workers

# A backend whose Stop rule is the stand-in query "query" (scan_pids is patched).
QUERY_BACKEND = SimpleNamespace(_patterns=lambda: "query")


def proc(root, pid, parent=1, maps="", device=False):
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "stat").write_text(f"{pid} (game with spaces) S {parent} 0 0")
    (entry / "maps").write_text(maps)
    (entry / "fd").mkdir()
    if device:
        (entry / "fd/3").symlink_to("/dev/dri/renderD128")


# ---- the X11 window list ---------------------------------------------------
def test_x11_windows_is_the_owned_window_list(monkeypatch):
    monkeypatch.setattr(x11windows, "list_windows_owned", lambda: [(16, 42), (32, 99)])
    assert launch_window.x11_windows() == {16: 42, 32: 99}


def test_x11_unavailable_or_failing_is_no_evidence(monkeypatch):
    monkeypatch.setattr(x11windows, "list_windows_owned", lambda: None)
    assert launch_window.x11_windows() == {}

    def boom():
        raise OSError("X server went away")

    monkeypatch.setattr(x11windows, "list_windows_owned", boom)
    assert launch_window.x11_windows() == {}


def test_owned_window_list_keeps_only_shown_real_windows_with_an_owner(monkeypatch):
    windows = [
        (16, "steam_proton", "Game", 42, True, (1280, 720)),
        (17, "steam_proton", "Default IME", 42, True, (1, 1)),  # Wine's helper window
        (18, "steam_proton", "Dialog", 42, False, (500, 300)),  # not on screen
        (19, "steam_proton", "No pid", None, True, (800, 600)),  # owner unknown
    ]
    monkeypatch.setattr(x11windows, "_list_windows_full", lambda: windows)
    assert x11windows.list_windows_owned() == [(16, 42)]


def test_the_shared_walk_still_feeds_the_existing_listings(monkeypatch):
    monkeypatch.setattr(
        x11windows,
        "_list_windows_full",
        lambda: [(16, "steam_proton", "NIKKE", 42, True, (800, 600))],
    )
    assert x11windows.list_windows_sized() == [("steam_proton", "NIKKE", True, (800, 600))]
    assert x11windows.list_windows() == [("steam_proton", "NIKKE", True)]


# ---- which processes own the window ---------------------------------------
def test_new_owned_x11_window_including_descendants(tmp_path):
    proc(tmp_path, 10)
    proc(tmp_path, 11, parent=10)
    probe = launch_window.WindowEvidence({100}, proc_root=tmp_path)
    assert not probe.seen([10], 1, {100: 10})  # there before Play
    assert not probe.seen([10], 1, {101: 99})  # another program
    assert probe.seen([10], 1, {102: 11})  # the game's launcher, a child
    assert probe.seen([10], 1, {103: 10})  # the game itself


def test_a_sandboxed_game_s_window_carries_its_namespace_pid(tmp_path):
    # Sandbox gives the game its own PID namespace (#1593): Wine writes the pid the game
    # sees (the last NSpid field) into _NET_WM_PID, not the host pid Stop uses.
    proc(tmp_path, 10)
    proc(tmp_path, 11, parent=10)
    (tmp_path / "10/status").write_text("Name:\tbwrap\nNSpid:\t10\n")
    (tmp_path / "11/status").write_text("Name:\tgame.exe\nNSpid:\t11\t7\n")
    probe = launch_window.WindowEvidence({100}, proc_root=tmp_path)
    assert probe.seen([10], 1, {102: 7})
    assert not probe.seen([10], 1, {103: 8})  # some other namespace pid
    assert not probe.seen([10], 1, {100: 7})  # still not a window from before Play


def test_the_window_walk_holds_one_lock(monkeypatch):
    # XSetErrorHandler is process-wide: two walks at once could restore the default
    # (exit on BadWindow) handler in the middle of the other one.
    seen = []

    class Lib:
        def __getattr__(self, name):
            return SimpleNamespace()

        def XOpenDisplay(self, _name):
            seen.append(x11windows._WALK_LOCK.locked())
            return 0

    monkeypatch.setattr(x11windows.ctypes, "CDLL", lambda _name: Lib())
    monkeypatch.setattr(x11windows, "_configure", lambda lib: None)
    assert x11windows._list_windows_full() is None
    assert seen == [True]
    assert not x11windows._WALK_LOCK.locked()


def test_a_new_window_counts_even_when_proc_cannot_be_read(tmp_path):
    probe = launch_window.WindowEvidence({100}, proc_root=tmp_path / "gone")
    assert probe.seen([10], 1, {101: 10})


def test_a_missing_proc_tree_is_not_evidence(tmp_path):
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    assert not probe.seen([42], launch_window.GRAPHICS_GRACE, {})


def test_graphics_evidence_needs_the_grace_and_a_launch_process(tmp_path):
    proc(tmp_path, 10)
    proc(tmp_path, 11, parent=10, device=True)
    proc(tmp_path, 99, maps="/usr/lib/dxgi.dll", device=True)  # not ours
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path)
    grace = launch_window.GRAPHICS_GRACE
    assert not probe.seen([10], grace - 0.1, {})
    assert probe.seen([10], grace, {})
    (tmp_path / "11/fd/3").unlink()
    assert not probe.seen([10], grace, {})
    (tmp_path / "11/maps").write_text("0000 /wine/d3d11.dll\n")
    assert probe.seen([10], grace, {})
    (tmp_path / "11/maps").write_text("0000 /usr/lib/libvulkan.so.1\n")
    assert not probe.seen([10], grace, {})


# ---- observing the existing launch ----------------------------------------
def test_worker_monitors_existing_launch_gate(monkeypatch):
    worker = workers.BackendWorker(Config(), "g", "launch")
    events = []
    worker.backend_event.connect(events.append)
    pids = lambda: [10]  # noqa: E731
    backend = SimpleNamespace(game_pids=lambda: pids)
    order = []

    @contextmanager
    def watch(actual, emit, cancel):
        assert actual is pids and cancel is worker._cancel_token
        order.append("watch")
        emit(workers.Event("window_seen"))
        yield
        order.append("end")

    monkeypatch.setattr(launch_window, "watch", watch)
    monkeypatch.setattr(workers.launchgate, "play", lambda b: order.append("play") or 7)
    assert worker._dispatch(backend) == 7
    assert order == ["watch", "play", "end"]
    assert [event.kind for event in events] == ["window_seen"]


def test_monitor_takes_the_baseline_before_the_launch_starts(monkeypatch):
    calls = []
    release = threading.Event()
    ready = threading.Event()
    emitted = []

    def windows():
        # The baseline must be taken on the caller's thread, before Play can start.
        calls.append(threading.current_thread().name)
        return {}

    monkeypatch.setattr(launch_window, "x11_windows", windows)

    def scan(query):
        release.wait(2)
        return [10]

    monkeypatch.setattr(wine, "scan_pids", scan)

    def seen(self, pids, elapsed, windows):
        assert pids == [10]
        ready.set()
        return True

    monkeypatch.setattr(launch_window.WindowEvidence, "seen", seen)
    with launch_window.watch(wine.game_pids(QUERY_BACKEND), emitted.append, Cancel()):
        # The baseline was captured synchronously: the monitor is still blocked in scan_pids.
        assert calls == ["MainThread"]
        release.set()
        assert ready.wait(2)
    assert [event.kind for event in emitted] == ["window_seen"]


def test_monitor_cancellation_suppresses_late_window(monkeypatch):
    ready = threading.Event()
    cancel = Cancel()
    emitted = []
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(wine, "scan_pids", lambda query: [10])

    def seen(*args):
        cancel.cancel()
        ready.set()
        return True

    monkeypatch.setattr(launch_window.WindowEvidence, "seen", seen)
    with launch_window.watch(wine.game_pids(QUERY_BACKEND), emitted.append, cancel):
        assert ready.wait(2)
    assert not emitted


def test_monitor_does_not_report_a_window_after_the_launch_ended(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    emitted = []
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(wine, "scan_pids", lambda query: [10])

    def seen(*args):
        entered.set()
        release.wait(2)
        return True

    monkeypatch.setattr(launch_window.WindowEvidence, "seen", seen)
    with launch_window.watch(wine.game_pids(QUERY_BACKEND), emitted.append, Cancel()):
        assert entered.wait(2)
    release.set()
    time.sleep(0.2)
    assert emitted == []


def test_monitor_without_a_process_rule_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_window, "x11_windows", lambda: calls.append("windows") or {})
    no_rule = wine.game_pids(SimpleNamespace())
    with launch_window.watch(no_rule, lambda _event: calls.append("event"), Cancel()):
        pass
    assert calls == []


def test_window_seen_stays_out_of_the_run_log():
    worker = workers.BackendWorker(Config(), "g", "launch")
    assert not worker._worth_logging(workers.Event("window_seen"))
    assert worker._worth_logging(workers.Event("step", "Starting the game"))


# ---- the process rule lives in the backend layer (core must not import it) ----
def test_a_backend_without_a_process_rule_offers_no_pids():
    from nadare.core.backends import GameBackend

    assert GameBackend.game_pids(SimpleNamespace()) is None


def test_wine_backends_watch_the_pids_their_stop_uses(monkeypatch):
    from nadare.backends.steam import SteamBackend
    from nadare.backends.winegame import WineGameBackend

    seen = []
    monkeypatch.setattr(wine, "scan_pids", lambda query: seen.append(query) or [5])
    for cls in (wine.WineBackend, WineGameBackend, SteamBackend):
        fake = SimpleNamespace(
            _patterns=lambda: "rule",
            _app_id=lambda: "123456",
            _matching_pids=lambda app_id: seen.append(app_id) or [5],
        )
        assert cls.game_pids(fake)() == [5]
    assert seen == ["rule", "rule", "123456"]


def test_a_broken_process_rule_offers_no_pids():
    def broken():
        raise OSError("no prefix")

    assert wine.game_pids(SimpleNamespace(_patterns=broken)) is None


def test_play_runs_when_the_backend_cannot_offer_pids(monkeypatch):
    # The window monitor is optional: a backend with no game_pids, or one that fails,
    # still launches (and is simply not watched).
    def broken():
        raise OSError("no prefix")

    monkeypatch.setattr(workers.launchgate, "play", lambda b: 3)
    seen = []

    @contextmanager
    def watch(actual, emit, cancel):
        seen.append(actual)
        yield

    monkeypatch.setattr(launch_window, "watch", watch)
    for backend in (SimpleNamespace(), SimpleNamespace(game_pids=broken)):
        worker = workers.BackendWorker(Config(), "g", "launch")
        assert worker._dispatch(backend) == 3
    assert seen == [None, None]


def test_namespace_pid_collision_does_not_select_another_game(tmp_path):
    proc_root = tmp_path / 'proc'
    for host_pid in (10000, 20000):
        entry = proc_root / str(host_pid)
        entry.mkdir(parents=True)
        (entry / 'stat').write_text(f'{host_pid} (game.exe) S 1 0 0')
        (entry / 'status').write_text(f'NSpid:\t{host_pid}\t42\n')
    # Window 101 belongs to game A (host PID 10000), 202 to game B (host PID 20000).
    # Both clients report their namespace-local PID 42 to the shared X server.
    windows = {101: 42, 202: 42}
    owned = launch_window.WindowEvidence(set(), proc_root=proc_root).owned_windows([10000], windows)
    assert owned == {}, 'an unqualified namespace PID must not select either window'
