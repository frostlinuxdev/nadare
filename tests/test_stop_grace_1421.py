# SPDX-License-Identifier: GPL-3.0-or-later
"""#1421 ask 3: Stop asks the game to close before it is killed.

The polite step runs ``taskkill`` (never ``/F``) in the game's prefix, waits a short grace for
the game to exit, and only then runs the existing SIGTERM -> SIGKILL path. These tests use a fake
clock and a fake process table; no Wine, Proton or taskkill runs.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame_parts import launching
from nadare.core import process
from nadare.core.events import Cancel, Cancelled, Event


def _backend(tmp_path: Path) -> tuple[NikkeBackend, list[Event]]:
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "NIKKE" / "nikke_launcher.exe", "MZ")
    wine = write_exe(tmp_path / "wine")
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"wine_binary": str(wine), "prefix": str(prefix)},
        )
    )
    events: list[Event] = []
    backend.ctx.emit = events.append
    return backend, events


def _fake_clock(monkeypatch, clock: list[float]) -> None:
    monkeypatch.setattr(launching.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(launching.time, "sleep", sleep)


def _patch_stop(monkeypatch, alive: set[int], calls: list[tuple[str, list[int]]]) -> None:
    def scan(*_args, **_kwargs):
        return sorted(alive)

    def stop_pids(pids, signal_=signal.SIGTERM):
        name = "term" if signal_ == signal.SIGTERM else "kill"
        calls.append((name, list(pids)))
        if signal_ == signal.SIGKILL:
            alive.clear()

    patch_everywhere(monkeypatch, wg, "scan_pids", scan)
    patch_everywhere(monkeypatch, wg, "stop_pids", stop_pids)
    patch_everywhere(monkeypatch, wg, "stop_runner_wineserver", lambda **_kw: None)


def _seed_polite_stop(
    backend: NikkeBackend,
    *,
    kind: str = "taskkill",
    commands: list[list[str]] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Stand in for the close command the launch saved, so Stop never builds one."""
    from nadare.core import stopstate

    if commands is None and kind == "taskkill":
        commands = [["taskkill", "/IM", "nikke_launcher.exe"]]
    stopstate.save(backend.prefix(), kind=kind, commands=commands or [], env=env or {})


def test_default_grace_is_eight_and_a_profile_key_overrides_it(tmp_path):
    backend, _events = _backend(tmp_path)
    assert launching.STOP_GRACE_SECONDS == 8.0
    assert backend._stop_grace_seconds() == 8.0
    backend.ctx.profile["stop_grace_seconds"] = 2.5
    assert backend._stop_grace_seconds() == 2.5
    backend.ctx.profile["stop_grace_seconds"] = "0"
    assert backend._stop_grace_seconds() == 0.0
    backend.ctx.profile["stop_grace_seconds"] = "not a number"
    assert backend._stop_grace_seconds() == 8.0


def test_stop_asks_waits_then_uses_the_existing_term_kill_path(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path)
    monkeypatch.setattr(launching.sandbox, "in_flatpak", lambda: True)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    helper_calls: list[tuple[list[str], dict[str, str], float]] = []
    signal_times = []
    original_stop = launching.stop_pids

    def timed_stop(pids, signal_=signal.SIGTERM):
        signal_times.append(clock[0])
        original_stop(pids, signal_)

    monkeypatch.setattr(launching, "stop_pids", timed_stop)

    def helper(argv, env, deadline):
        assert calls == [], "a signal was sent before taskkill"
        helper_calls.append((list(argv), dict(env), deadline))
        return True

    monkeypatch.setattr(backend, "_run_stop_helper", helper)
    backend.stop()

    assert [name for name, _ in calls] == ["term", "kill"]
    assert helper_calls, "the polite taskkill was never run"
    assert helper_calls[0][0][:2] == ["taskkill", "/IM"]
    assert "Asking the game to close…" in [
        event.message for event in events if event.kind == "step"
    ]
    # Polite ran before the first signal, and SIGTERM before SIGKILL.
    assert signal_times == [108.0, 108.5]


def test_grace_zero_skips_the_polite_step(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path)
    backend.ctx.profile["stop_grace_seconds"] = 0
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)

    def helper(*_args, **_kwargs):
        pytest.fail("grace 0 must not run taskkill")

    monkeypatch.setattr(backend, "_run_stop_helper", helper)
    backend.stop()

    assert calls == [("term", [7])]
    assert clock[0] == 100.0
    assert not [event for event in events if event.kind == "step"]


def test_a_game_that_exits_during_the_grace_is_not_signalled(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, "_run_stop_helper", lambda *_a, **_k: True)

    original_sleep = launching.time.sleep

    def sleep(seconds):
        original_sleep(seconds)
        alive.clear()  # the game saved and quit while we waited

    monkeypatch.setattr(launching.time, "sleep", sleep)
    backend.stop()

    assert calls == [], "a game that closed by itself must not get SIGTERM"
    assert "Asking the game to close…" in [
        event.message for event in events if event.kind == "step"
    ]


def test_a_helper_that_cannot_run_escalates_immediately(tmp_path, monkeypatch):
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, "_run_stop_helper", lambda *_a, **_k: False)
    backend.stop()

    assert calls == [("term", [7])]
    assert clock[0] == 100.0, "a failed taskkill must not wait out the grace"


def test_one_missing_exe_does_not_cancel_the_grace(tmp_path, monkeypatch):
    """A launcher-only session also has the game exe in the launch query; it is not running."""
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(
        backend,
        commands=[
            ["taskkill", "/IM", "nikke.exe"],
            ["taskkill", "/IM", "nikke_launcher.exe"],
        ],
    )

    def helper(argv, _env, _deadline):
        return argv[-1] == "nikke_launcher.exe"  # the game is not running; the launcher is

    monkeypatch.setattr(backend, "_run_stop_helper", helper)
    original_sleep = launching.time.sleep

    def sleep(seconds):
        original_sleep(seconds)
        alive.clear()  # the launcher closed during the grace

    monkeypatch.setattr(launching.time, "sleep", sleep)
    backend.stop()

    assert calls == [], "a missing extra exe must not skip the launcher's grace"
    assert clock[0] == 100.5


def test_a_second_stop_escalates_at_once(tmp_path, monkeypatch):
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, "_run_stop_helper", lambda *_a, **_k: True)

    original_sleep = launching.time.sleep

    def sleep(seconds):
        original_sleep(seconds)
        backend.ctx.cancel.cancel()  # the user pressed Stop again
        backend.ctx.cancel.cancel()

    monkeypatch.setattr(launching.time, "sleep", sleep)
    backend.stop()

    assert calls == [("term", [7])]
    assert clock[0] < 100.0 + launching.STOP_GRACE_SECONDS


def test_a_polite_close_is_not_repeated_on_one_backend(tmp_path, monkeypatch):
    """The run-lock stop and the cancelled launch share a backend: one taskkill, one grace."""
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    helper_calls: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
    )

    assert backend._ask_game_to_close([7]) == [7]
    first_count = len(helper_calls)
    assert first_count == 1
    clock[0] = 500.0
    assert backend._ask_game_to_close([7]) == [7]

    assert len(helper_calls) == first_count
    assert clock[0] == 500.0, "a second polite wait ran"


def test_stop_with_no_saved_command_skips_the_polite_step(tmp_path, monkeypatch):
    """A game started before this version, or by another path, has no saved request."""
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    monkeypatch.setattr(
        backend,
        "_prefix_command",
        lambda *_a, **_k: pytest.fail("Stop must not build a launch command"),
    )
    monkeypatch.setattr(
        backend, "_run_stop_helper", lambda *_a, **_k: pytest.fail("no command to run")
    )
    backend.stop()

    assert calls == [("term", [7])]
    assert clock[0] == 100.0


def test_the_saved_taskkill_command_comes_from_the_launch_and_is_never_forced(
    tmp_path, monkeypatch
):
    from nadare.core import stopstate

    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: False)
    monkeypatch.setattr(
        backend, "_stop_exe_names", lambda: ["nikke_launcher.exe", "nikke.exe"]
    )
    env = {"WINEPREFIX": "/games/pfx", "GAME_OPTION": "value with spaces"}
    backend._record_polite_stop(["/runner/proton", "run", "nikke_launcher.exe"], env, 1)

    saved = stopstate.load(backend.prefix())
    assert saved is not None and saved["kind"] == "taskkill"
    assert saved["env"] == env
    commands = saved["commands"]
    assert len(commands) == 2
    assert commands[0] == [
        "/runner/proton",
        "run",
        "taskkill",
        "/IM",
        "nikke_launcher.exe",
    ]
    for argv in commands:
        assert "/IM" in argv
        assert not any(part.lower() == "/f" for part in argv)


def test_the_saved_state_keeps_only_the_launch_env_delta_and_drops_secrets(
    tmp_path, monkeypatch
):
    """R1: the private state file must not hold the player's whole environment."""
    from nadare.core import stopstate

    backend, _events = _backend(tmp_path)
    monkeypatch.setenv("HOME", "/home/player")
    monkeypatch.setenv("NADARE_TEST_TOKEN", "secret-value")
    env = dict(os.environ)  # the launch starts from the process environment
    env["WINEPREFIX"] = "/games/pfx"
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    env["NADARE_TEST_SESSION_ID"] = "private"  # added, secret-looking: dropped
    env["PROTON_KEY"] = "kept"  # secret-looking, but the Proton family is kept
    env["NADARE_TEST_ADDED"] = "1"

    stopstate.save(
        backend.prefix(),
        kind="taskkill",
        commands=[["taskkill", "/IM", "nikke_launcher.exe"]],
        env=env,
    )

    raw = json.loads(stopstate.path(backend.prefix()).read_text(encoding="utf-8"))
    assert set(raw["env"]) == {
        "WINEPREFIX",
        "WINEDLLOVERRIDES",
        "PROTON_KEY",
        "NADARE_TEST_ADDED",
    }
    assert raw["env"]["WINEPREFIX"] == "/games/pfx"
    assert raw["env"]["WINEDLLOVERRIDES"] == "mscoree,mshtml="
    assert "HOME" not in raw["env"], "an unchanged variable must not be persisted"
    assert "NADARE_TEST_TOKEN" not in raw["env"]
    assert "NADARE_TEST_SESSION_ID" not in raw["env"]


def test_stop_runs_the_helper_in_the_launch_env_plus_the_saved_delta(
    tmp_path, monkeypatch
):
    """The state file holds a delta; Stop restores it over the environment Play built.

    The helper must not inherit this process's environment: that is where the credentials
    and Python/loader variables a launch filters out would come back (#1421 review).
    """
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    monkeypatch.setenv("NADARE_TEST_STOP_ONLY", "/from/stop")
    monkeypatch.setenv("WINEDLLOVERRIDES", "from-stop")
    _seed_polite_stop(
        backend, env={"WINEPREFIX": "/p", "WINEDLLOVERRIDES": "from-launch"}
    )
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda _argv, env, _deadline: seen.append(dict(env)) or True,
    )

    backend.stop()

    assert seen, "the polite helper never ran"
    env = seen[0]
    assert env["WINEPREFIX"] == "/p", "a variable added at launch must be restored"
    assert "NADARE_TEST_STOP_ONLY" not in env, (
        "a host variable the launch never passed must not reach the helper"
    )
    assert env["WINEDLLOVERRIDES"] == "from-launch", "the launch's changed value wins"


def test_a_sandboxed_launch_saves_an_x11_request(tmp_path, monkeypatch):
    from nadare.core import stopstate


    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    monkeypatch.setattr(
        backend, "_stop_exe_names", lambda: pytest.fail("no taskkill for a Sandboxed game")
    )

    backend._record_polite_stop(["wine", "game.exe"], {"WINEPREFIX": "/p"}, 1)

    saved = stopstate.load(backend.prefix())
    assert saved == {
        "version": stopstate.VERSION,
        "kind": "x11",
        "commands": [],
        "env": {},
    }


def test_cancel_counts_presses():
    cancel = Cancel()
    assert cancel.cancel_count == 0
    cancel.cancel()
    assert cancel.cancel_count == 1
    cancel.cancel()
    assert cancel.cancel_count == 2


def test_run_asks_before_it_signals_the_child(tmp_path, monkeypatch):
    """A launch cancelled by Stop runs the polite step before any SIGTERM reaches the game."""
    go = tmp_path / "go"
    term = tmp_path / "term"
    script = (
        "import pathlib, signal, time\n"
        "def onterm(*_):\n"
        f"    pathlib.Path({str(term)!r}).write_text('term')\n"
        "    raise SystemExit(42)\n"
        "signal.signal(signal.SIGTERM, onterm)\n"
        f"while not pathlib.Path({str(go)!r}).exists():\n"
        "    time.sleep(0.02)\n"
    )
    cancel = Cancel()
    seen: list[str] = []

    def before_stop():
        seen.append("polite")
        go.write_text("go")
        time.sleep(0.5)  # the game exits while the polite step waits

    import threading

    def press_stop():
        time.sleep(0.1)
        cancel.cancel()

    thread = threading.Thread(target=press_stop, daemon=True)
    thread.start()
    try:
        with pytest.raises(Cancelled):
            process.run(
                [sys.executable, "-c", script],
                cancel=cancel,
                before_stop=before_stop,
            )
    finally:
        thread.join(timeout=2)

    assert seen == ["polite"]
    assert not term.exists(), "the child got SIGTERM before the polite step finished"


def _fake_x11(monkeypatch, windows, sent):
    from nadare.core import x11windows

    monkeypatch.setattr(x11windows, "list_windows_owned", lambda: windows)

    def close(ids):
        sent.append(list(ids))
        return len(ids)

    monkeypatch.setattr(x11windows, "request_close", close)
    return x11windows


def test_a_sandboxed_game_is_closed_through_x11_without_a_helper(tmp_path, monkeypatch):
    # The Sandbox's wineserver lives in the Sandbox's /tmp, so no taskkill may start outside it.
    # The game's own windows are asked to close instead, then the existing signals wait.
    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend, kind="x11")
    sent: list[list[int]] = []
    _fake_x11(monkeypatch, [(11, 7), (12, 99), (13, 7)], sent)
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda *_a, **_k: pytest.fail("a Sandboxed game must not get a taskkill helper"),
    )

    backend.stop()

    assert sent == [[11, 13]], "only the game's own windows are asked to close"
    assert calls == [("term", [7])]
    assert clock[0] == 108.0, "the grace still runs after the close request"


@pytest.mark.parametrize(
    ("windows", "display", "reason"),
    [
        ([], "1", "no X11 window"),
        (None, "1", "X11 is not available"),
        (None, None, "no X display"),
    ],
)
def test_a_sandboxed_game_without_a_window_goes_straight_to_signals(
    tmp_path, monkeypatch, windows, display, reason
):
    backend, events = _backend(tmp_path)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend, kind="x11")
    sent: list[list[int]] = []
    _fake_x11(monkeypatch, windows, sent)
    if display is None:
        monkeypatch.delenv("DISPLAY", raising=False)
    else:
        monkeypatch.setenv("DISPLAY", display)

    backend.stop()

    assert sent == []
    assert calls == [("term", [7])]
    assert clock[0] == 100.0, "no window means no grace"
    logged = [event.message for event in events if event.kind == "log"]
    assert any(reason in message for message in logged), logged


def test_stop_uses_the_saved_command_without_a_builder(tmp_path, monkeypatch):
    """No launch preparation may run at Stop time, however slow it would be."""
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(
        backend,
        commands=[["wine", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={"WINEPREFIX": "/p"},
    )

    def slow_builder(*_args, **_kwargs):
        clock[0] += 90.0  # locale generation, runtime probing, a download...
        pytest.fail("Stop built a launch command")

    monkeypatch.setattr(backend, "_prefix_command", slow_builder)
    monkeypatch.setattr(
        backend, "_stop_exe_names", lambda: pytest.fail("Stop looked for game executables")
    )
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda argv, env, _deadline: seen.append((list(argv), dict(env))) or True,
    )

    backend.stop()

    assert [argv for argv, _env in seen] == [
        ["wine", "taskkill", "/IM", "nikke_launcher.exe"]
    ]
    helper_env = seen[0][1]
    assert helper_env["WINEPREFIX"] == "/p"
    assert helper_env.get("PATH") == os.environ.get("PATH"), "Stop's environment is the base"
    assert calls == [("term", [7])]
    assert clock[0] <= 100.0 + launching.STOP_GRACE_SECONDS + 1.0


def test_launcher_only_stop_waits_like_the_standalone_path(tmp_path, monkeypatch):
    """Only the launcher runs: both Stop paths must still use the launcher query."""
    backend, _events = _backend(tmp_path)
    backend.game_process_names = ("game.exe",)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls: list[tuple[str, list[int]]] = []
    _seed_polite_stop(backend)

    def scan(query, *_args, **_kwargs):
        return [] if getattr(query, "require_name", False) else [7]

    patch_everywhere(monkeypatch, wg, "scan_pids", scan)
    patch_everywhere(
        monkeypatch,
        wg,
        "stop_pids",
        lambda pids, signal_=signal.SIGTERM: calls.append(
            ("term" if signal_ == signal.SIGTERM else "kill", list(pids))
        ),
    )
    patch_everywhere(monkeypatch, wg, "stop_runner_wineserver", lambda **_kw: None)
    helper_calls: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
    )

    # The launch-cancel hook (no pids passed) must not skip a running launcher.
    assert backend._ask_game_to_close() == [7]
    assert helper_calls == [["taskkill", "/IM", "nikke_launcher.exe"]]
    assert clock[0] == 108.0

    # A fresh Stop worker reaches the same query and the same wait.
    other, _ = _backend(tmp_path)
    other.game_process_names = ("game.exe",)
    _seed_polite_stop(other)
    clock[0] = 200.0
    helper_calls.clear()
    monkeypatch.setattr(
        other,
        "_run_stop_helper",
        lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
    )
    other.stop()
    assert helper_calls == [["taskkill", "/IM", "nikke_launcher.exe"]]
    assert calls == [("term", [7])]
    assert clock[0] == 208.0


def test_long_lived_wine_helpers_do_not_hold_the_grace(tmp_path, monkeypatch):
    backend, _events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    pids = {7, 8}
    _seed_polite_stop(backend)
    monkeypatch.setattr(launching, "_wine_helper", lambda pid: pid == 8)

    def scan(*_args, **_kwargs):
        return sorted(pids)

    monkeypatch.setattr(wg, "scan_pids", scan)
    monkeypatch.setattr(launching, "scan_pids", scan)

    def helper(*_args, **_kwargs):
        pids.discard(7)  # the game closes; the helper stays
        return True

    monkeypatch.setattr(backend, "_run_stop_helper", helper)

    assert backend._ask_game_to_close() == [8]
    assert clock[0] == 100.0, "a Wine helper must not keep the grace running"


def test_owned_windows_keeps_host_tree_but_skips_namespace_pids(tmp_path):
    from nadare.core.launch_window import WindowEvidence

    def proc(pid, parent=1, nspid=None):
        entry = tmp_path / str(pid)
        entry.mkdir(parents=True)
        (entry / "stat").write_text(f"{pid} (game with spaces) S {parent} 0 0")
        if nspid is not None:
            (entry / "status").write_text(f"Name:\tgame.exe\nNSpid:\t{nspid}\n")

    proc(10)
    proc(11, parent=10)
    proc(12, parent=11, nspid="12 3")
    probe = WindowEvidence(set(), proc_root=tmp_path)

    assert probe.owned_windows([10], {1: 10, 2: 11, 3: 12, 4: 3, 5: 99}) == {
        1: 10,
        2: 11,
        3: 12,
    }


def test_the_close_message_goes_to_the_root_for_each_window():
    import ctypes

    from nadare.core import x11windows

    sent = []

    class Lib:
        def XSendEvent(self, display, root, propagate, mask, event):  # noqa: N802
            message = ctypes.cast(event, ctypes.POINTER(x11windows._XEvent)).contents
            sent.append(
                (
                    display,
                    root,
                    propagate,
                    mask,
                    message.xclient.send_event,
                    message.xclient.window,
                    message.xclient.message_type,
                    message.xclient.format,
                    list(message.xclient.data.l),
                )
            )
            return 1

    lib = Lib()
    assert x11windows._send_close(lib, 5, 42, 11, 77) is True
    assert sent == [
        (
            5,
            42,
            0,
            x11windows._SUBSTRUCTURE_REDIRECT | x11windows._SUBSTRUCTURE_NOTIFY,
            1,
            11,
            77,
            32,
            [0, 1, 0, 0, 0],
        )
    ]


def test_request_close_interns_the_ewmh_atom_and_sends_to_root(monkeypatch):
    import ctypes

    from nadare.core import x11windows

    monkeypatch.setenv("DISPLAY", ":1")
    calls = []

    class Lib:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: 0

        def XOpenDisplay(self, _name):  # noqa: N802
            return 1

        def XSetErrorHandler(self, _handler):  # noqa: N802
            return 0

        def XDefaultRootWindow(self, _display):  # noqa: N802
            return 42

        def XInternAtom(self, _display, name, _only_if_exists):  # noqa: N802
            calls.append(("atom", name))
            return 77

        def XSendEvent(self, _display, root, _propagate, _mask, event):  # noqa: N802
            message = ctypes.cast(event, ctypes.POINTER(x11windows._XEvent)).contents
            calls.append(("send", root, message.xclient.window, message.xclient.message_type))
            return 1

        def XFlush(self, _display):  # noqa: N802
            calls.append(("flush",))

    monkeypatch.setattr(x11windows.ctypes, "CDLL", lambda _name: Lib())
    monkeypatch.setattr(x11windows, "_configure", lambda _lib: None)

    assert x11windows.request_close([11, 13]) == 2
    assert calls == [
        ("atom", b"_NET_CLOSE_WINDOW"),
        ("send", 42, 11, 77),
        ("send", 42, 13, 77),
        ("flush",),
    ]


def test_close_request_without_a_display_is_unknown(monkeypatch):
    from nadare.core import x11windows

    monkeypatch.delenv("DISPLAY", raising=False)
    assert x11windows.request_close([11]) is None


def test_a_launch_saves_the_close_command_for_stop(tmp_path, monkeypatch):
    import contextlib
    import threading
    from types import SimpleNamespace

    from nadare.core import stopstate

    backend, _events = _backend(tmp_path)
    target = backend.find_launcher()
    monkeypatch.setattr(backend, "direct_mode", lambda: False)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: False)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda **_kw: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda **_kw: None)
    monkeypatch.setattr(
        backend, "build_launch", lambda: (["wine", "nikke_launcher.exe"], {}, target)
    )
    monkeypatch.setattr(backend, "_runtime_note", lambda: "")
    monkeypatch.setattr(backend, "_log_launch_details", lambda _env: None)
    monkeypatch.setattr(backend, "_begin_game_output", lambda: None)
    monkeypatch.setattr(backend, "_end_game_output", lambda: None)
    monkeypatch.setattr(backend, "_stop_link_relay", lambda: None)
    probe = SimpleNamespace(result="", stop=lambda: None, join=lambda **_kw: None)
    probe.start = lambda: probe
    monkeypatch.setattr(backend, "_renderer_probe", lambda: probe)
    monkeypatch.setattr(
        backend,
        "_game_process_watch",
        lambda: contextlib.nullcontext(threading.Event()),
    )
    monkeypatch.setattr(launching, "stream", lambda *_a, **_kw: 0)

    assert backend.launch() == 0
    saved = stopstate.load(backend.prefix())
    assert saved is not None and saved["kind"] == "taskkill"
    assert ["wine", "taskkill", "/IM", "nikke_launcher.exe"] in saved["commands"]
    assert all(argv[:2] == ["wine", "taskkill"] for argv in saved["commands"])


@pytest.mark.parametrize("value", [float("inf"), "Infinity", "NaN", 10**400])
def test_invalid_numeric_grace_uses_default(tmp_path, value):
    backend, _ = _backend(tmp_path)
    backend.ctx.profile["stop_grace_seconds"] = value
    assert backend._stop_grace_seconds() == launching.STOP_GRACE_SECONDS


def test_stop_worker_escalates_on_its_first_cancel(tmp_path, monkeypatch):
    # A standalone Stop worker starts with count 0; its first cancel is the second Stop.
    backend, _ = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls = []
    _patch_stop(monkeypatch, alive, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, "_run_stop_helper", lambda *_a: True)

    def sleep(seconds):
        clock[0] += seconds
        backend.ctx.cancel.cancel()

    monkeypatch.setattr(launching.time, "sleep", sleep)
    backend.stop()
    assert calls == [("term", [7])]
    assert clock[0] == 100.5


def test_expired_helper_is_not_spawned(tmp_path, monkeypatch):
    backend, _ = _backend(tmp_path)
    _fake_clock(monkeypatch, [100.0])
    monkeypatch.setattr(
        process, "spawn", lambda *_a, **_kw: pytest.fail("deadline already expired")
    )
    assert not backend._run_stop_helper(["taskkill"], {}, 99.0)


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "second_stop", "spawn_error"])
def test_real_helper_control_flow(tmp_path, monkeypatch, outcome):
    import subprocess

    backend, _ = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls = []

    class Helper:
        code = None
        pid = 1234

        def wait(self, timeout):
            calls.append("wait")
            if outcome == "timeout" and self.code is None:
                clock[0] = round(clock[0] + (timeout if timeout > 0 else 0.1), 6)
                raise subprocess.TimeoutExpired("taskkill", timeout)
            self.code = 0 if outcome == "success" else 1
            return self.code

        def poll(self):
            return self.code

        def kill(self):
            calls.append("kill")
            self.code = -9

    def spawn(*_args, **_kwargs):
        if outcome == "spawn_error":
            raise OSError("missing runner")
        if outcome == "second_stop":
            backend.ctx.cancel.cancel()
            backend.ctx.cancel.cancel()
        return Helper()

    monkeypatch.setattr(process, "spawn", spawn)
    groups = []
    monkeypatch.setattr(launching.os, "killpg", lambda *args: groups.append(args))
    assert backend._run_stop_helper(["taskkill"], {}, 105.0) is (outcome == "success")
    assert ("kill" in calls) is (outcome in ("timeout", "second_stop"))
    assert groups == ([] if outcome == "spawn_error" else [(1234, signal.SIGKILL)])
    assert clock[0] <= 105.0


@pytest.mark.parametrize("operation", ["launch", "stop"])
def test_controller_delivers_repeated_stop(qtbot, tmp_path, operation):
    from types import SimpleNamespace

    from nadare.core.config import Config
    from nadare.gui.workers import BackendController

    controller = BackendController(Config.load(tmp_path / "config.json"))
    cancel = Cancel()
    worker = SimpleNamespace(operation=operation, isRunning=lambda: True, cancel=cancel.cancel)
    controller._workers["p"] = worker
    try:
        assert controller.start("p", "stop")
        assert controller.start("p", "stop")
        assert cancel.cancel_count == 2
        if operation == "launch":
            assert controller._pending["p"] == ("stop", True)
    finally:
        controller._workers.clear()


def test_stopping_ui_allows_immediate_second_stop(qtbot, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.data.update(check_updates=False, check_notices=False)
    config.add_profile("p", backend="launcher-nikke", title="Game")
    window = MainWindow(config)
    qtbot.addWidget(window)
    window.open_game("p")
    window._on_busy("p", "launch", True)
    window._on_busy("p", "stop", True)
    window._on_event("p", Event("step", "Asking the game to close…"))
    actions = []
    window.game.action_requested.connect(actions.append)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_kw: pytest.fail("second Stop must escalate")
    )
    assert not window.game.stop_button.isHidden()
    assert window.game.stop_button.isEnabled()
    assert window.game.step_label.text() == "Asking the game to close…"
    window.game.stop_button.click()
    assert actions == ["stop"]
    window.close()


def test_helper_descendant_cannot_run_after_grace(tmp_path, monkeypatch):
    backend, _ = _backend(tmp_path)
    marker = tmp_path / 'helper-ran-after-grace'
    child = (
        'import time, pathlib; time.sleep(0.6); pathlib.Path('
        + repr(str(marker)) + ').write_text("late")'
    )
    wrapper = (
        'import subprocess, sys; subprocess.Popen([sys.executable, "-c", '
        + repr(child) + '])'
    )
    spawned = []
    real_spawn = process.spawn

    def track(*args, **kwargs):
        proc = real_spawn(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(process, 'spawn', track)
    try:
        deadline = time.monotonic() + 0.3
        assert backend._run_stop_helper([sys.executable, '-c', wrapper], dict(os.environ), deadline)
        time.sleep(0.9)
        assert not marker.exists(), 'helper child continued beyond the grace after wrapper exit'
    finally:
        for proc in spawned:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=1)



def test_second_stop_before_standalone_grace_skips_wait(tmp_path, monkeypatch):
    backend, _ = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls = []
    _patch_stop(monkeypatch, {7}, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, '_run_stop_helper', lambda *_a: True)
    # First Stop created this standalone Stop worker. Second Stop cancels it
    # before it gets to _ask_game_to_close (e.g. during the first process scan).
    backend.ctx.cancel.cancel()
    backend.stop()
    assert clock[0] == 100.0, 'second Stop still waited a complete grace'


@pytest.mark.parametrize("presses", [1, 2])
def test_queued_stop_preserves_launch_escalation(qtbot, tmp_path, monkeypatch, presses):
    from nadare.core.config import Config
    from nadare.gui.workers import BackendController, BackendWorker

    backend, _ = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls = []
    _patch_stop(monkeypatch, {7}, calls)
    _seed_polite_stop(backend)
    monkeypatch.setattr(backend, "_run_stop_helper", lambda *_a: True)
    monkeypatch.setattr(BackendWorker, "start", lambda self: None)
    controller = BackendController(Config.load(tmp_path / "config.json"))
    launch = BackendWorker(controller._config, "p", "launch", parent=controller)
    controller._workers["p"] = launch
    monkeypatch.setattr(launch, "isRunning", lambda: True)
    try:
        for _ in range(presses):
            assert controller.start("p", "stop")
        monkeypatch.setattr(launch, "isRunning", lambda: False)
        controller._on_worker_finished("p", launch)
        queued = controller.worker("p")
        assert queued.operation == "stop"
        backend.ctx.cancel = queued._cancel_token
        queued._dispatch(backend)
        assert calls == [("term", [7])]
        assert clock[0] == (108.0 if presses == 1 else 100.0)
    finally:
        controller._workers.clear()
