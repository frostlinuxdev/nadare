# SPDX-License-Identifier: GPL-3.0-or-later
"""Playing follows live processes during a launch (#1609, #1615)."""

from __future__ import annotations

import threading

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import winegame
from nadare.backends.direct import BrownDust2Backend, NteDirectBackend
from nadare.backends.wine import _query_matches
from nadare.backends.winegame_parts import launching
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.events import Event


@pytest.fixture
def backend(tmp_path, monkeypatch):
    backend = NteDirectBackend(make_context(tmp_path, NteDirectBackend.kind))
    write_exe(backend.direct_target())
    monkeypatch.setattr(backend, "_runner_missing", lambda: False)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: True)
    monkeypatch.setattr(winegame.graphics, "vulkan_note", lambda: "")
    return backend


@pytest.mark.parametrize("name,running", [("NTEGlobalGame.exe", False), ("HTGame.exe", True)])
def test_nte_status_only_counts_game(backend, monkeypatch, name, running):
    env = {"WINEPREFIX": str(backend.prefix() / "pfx")}
    monkeypatch.setattr(
        winegame, "scan_pids", lambda query: [123] if _query_matches(query, [name], env) else []
    )
    assert backend.status().running is running


def test_other_backend_still_counts_prefix(tmp_path, monkeypatch):
    backend = BrownDust2Backend(make_context(tmp_path, BrownDust2Backend.kind))
    write_exe(backend.direct_target())
    monkeypatch.setattr(backend, "_runner_missing", lambda: False)
    monkeypatch.setattr(winegame.graphics, "vulkan_note", lambda: "")
    monkeypatch.setattr(
        winegame,
        "scan_pids",
        lambda query: (
            [123]
            if _query_matches(query, ["wineserver"], {"WINEPREFIX": str(backend.prefix())})
            else []
        ),
    )
    assert backend.status().running


def test_nte_stop_keeps_prefix_query(backend, monkeypatch):
    queries, stopped, servers = [], [], []
    monkeypatch.setattr(launching, "scan_pids", lambda query: queries.append(query) or [123])
    monkeypatch.setattr(backend, "_stop_game_processes", stopped.append)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    monkeypatch.setattr(launching, "stop_runner_wineserver", lambda **kw: servers.append(kw))
    backend.stop()
    assert _query_matches(queries[0], ["wineserver"], {"WINEPREFIX": str(backend.prefix())})
    assert stopped == [[123]] and len(servers) == 1


@pytest.mark.parametrize("seen", [True, False])
def test_watcher_stops_only_after_seen_then_gone(backend, monkeypatch, seen):
    events, stopped, logs = [], [], []
    backend.ctx.emit = events.append
    monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(backend, "info", logs.append)
    scans = iter(([123], [], [123], [], [], [], [], []) if seen else ([],) * 8)
    monkeypatch.setattr(launching, "scan_pids", lambda query: next(scans))
    now = [0.0]
    monkeypatch.setattr(launching.time, "monotonic", lambda: now[0])

    class Done:
        def wait(self, interval):
            assert 0.5 <= interval <= 2
            now[0] += 1
            return now[0] > 8

    closed = threading.Event()
    backend._watch_game_process(Done(), closed)
    assert stopped == ([True] if seen else [])
    assert closed.is_set() is seen
    assert len(logs) == int(seen)
    assert "game closed" in logs[0] if seen else not logs
    assert [event.kind for event in events] == (["game_running"] if seen else [])


def test_watcher_cancel_does_not_stop(backend, monkeypatch):
    backend.ctx.cancel.cancel()
    monkeypatch.setattr(backend, "stop", lambda: pytest.fail("cancel must own stop"))
    monkeypatch.setattr(launching, "scan_pids", lambda query: pytest.fail("cancelled"))
    backend._watch_game_process(threading.Event(), threading.Event())


def test_busy_launch_updates_sidebar_card_and_played_once(tmp_path, qtbot, monkeypatch):
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.data.update(check_updates=False, check_notices=False)
    config.add_profile("g", backend="wine", title="Game")
    window = MainWindow(config)
    qtbot.addWidget(window)
    monkeypatch.setattr(window.controller, "start", lambda *a, **kw: True)
    window.open_game("g")
    window._on_result("g", "status", Status("installed"))
    window._on_busy("g", "launch", True)
    calls = []
    original = config.mark_played
    monkeypatch.setattr(config, "mark_played", lambda name: calls.append(name) or original(name))
    assert not config.profiles["g"].get("last_played")
    for _ in range(2):
        window._on_event("g", Event("game_running"))
    assert calls == ["g"]
    assert config.profiles["g"]["last_played"]
    assert window._busy["g"] == "launch"
    assert not window.sidebar_games._rows["g"].playing_label.isHidden()
    assert window.library._cards["g"].property("running") is True
    window.close()


@pytest.mark.parametrize("playing,launch", [(True, True), (False, True), (True, False)])
def test_czn_stream_reports_real_start_once(tmp_path, monkeypatch, playing, launch):
    from types import SimpleNamespace

    from nadare.backends.czn.backend import CznBackend
    from nadare.backends.czn.backend_parts import streaming

    backend = CznBackend(make_context(tmp_path, CznBackend.kind))
    events = []
    backend.ctx.emit = events.append
    monkeypatch.setattr(backend, "_game_running", lambda: playing)
    pipe = SimpleNamespace(fileno=lambda: 42, close=lambda: None)
    monkeypatch.setattr(
        streaming.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(stdout=pipe, poll=lambda: None, wait=lambda: 0),
    )
    ticks = [0]

    def select(*args):
        ticks[0] += 1
        if ticks[0] == 5:
            raise ValueError("finished")
        return [], [], []

    monkeypatch.setattr(streaming.select, "select", select)
    monkeypatch.setattr(streaming.time, "monotonic", lambda: ticks[0])
    assert backend._stream(["fake"], {}, kill_game=launch) == 0
    assert sum(event.kind == "game_running" for event in events) == int(playing and launch)


@pytest.mark.parametrize("direct", [True, False])
def test_launch_watch_is_daemon_ends_with_launch_and_returns_normally(backend, monkeypatch, direct):
    from types import SimpleNamespace

    target = backend.direct_target()
    monkeypatch.setattr(backend, "direct_mode", lambda: direct)
    monkeypatch.setattr(backend, "find_launcher", lambda: target)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda **kw: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda **kw: None)
    monkeypatch.setattr(backend, "_direct_argv", lambda target: [str(target)])
    monkeypatch.setattr(backend, "_prefix_command", lambda *a, **kw: (["fake"], {}))
    monkeypatch.setattr(backend, "_secure_wrap", lambda argv, env: (argv, env))
    monkeypatch.setattr(backend, "build_launch", lambda: (["fake"], {}, target))
    monkeypatch.setattr(backend, "_runtime_note", lambda: "fake runner")
    monkeypatch.setattr(backend, "_log_launch_details", lambda env: None)
    monkeypatch.setattr(backend, "_begin_game_output", lambda: None)
    monkeypatch.setattr(backend, "_end_game_output", lambda: None)
    monkeypatch.setattr(backend, "_stop_link_relay", lambda: None)
    probe = SimpleNamespace(result="", stop=lambda: None, join=lambda **kw: None)
    probe.start = lambda: probe
    monkeypatch.setattr(backend, "_renderer_probe", lambda: probe)
    ready = threading.Event()
    threads = []

    def watch(done, closed):
        threads.append(threading.current_thread())
        closed.set()  # The game watcher finished Stop; its wrapper may exit nonzero.
        ready.set()
        done.wait(0.1)

    monkeypatch.setattr(backend, "_watch_game_process", watch)

    def stream(*args, **kwargs):
        assert ready.wait(0.1)
        return 137

    monkeypatch.setattr(launching, "stream", stream)
    assert winegame.WineGameBackend.launch(backend) == 0
    assert len(threads) == 1 and threads[0].daemon and not threads[0].is_alive()


@pytest.mark.parametrize("prefix", ["other", "same"])
def test_named_game_query_preserves_prefix_ownership(backend, prefix):
    env = {"WINEPREFIX": str(backend.prefix() if prefix == "same" else "/other/prefix")}
    assert _query_matches(backend._running_patterns(), [r"C:\NTE\HTGame.exe"], env) is (
        prefix == "same"
    )


def test_failed_launch_does_not_report_played(backend, monkeypatch):
    from nadare.core.backends import BackendError

    backend.direct_target().unlink()
    events = []
    backend.ctx.emit = events.append
    monkeypatch.setattr(
        backend, "_game_process_watch", lambda: pytest.fail("failed launch must not watch")
    )
    with pytest.raises(BackendError, match="executable not found"):
        winegame.WineGameBackend.launch(backend)
    assert not any(event.kind == "game_running" for event in events)
