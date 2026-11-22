# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions from the second adversarial round on #1421 (gentle Stop).

Each test shows one defect the first round shipped: a chatty game deadlocking Stop's polite
step, the helper inheriting host secrets, descendants killed without a shutdown chance, a
single Stop waiting two graces, the NIKKE tray cleanup disabling polite stop, a stale close
request, and a Sandbox fallback recorded as sandboxed. See REPORT-codexfix.md.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _backend_helpers import make_context, write_exe  # noqa: E402
from _parts_patch import patch_everywhere  # noqa: E402

from nadare.backends import winegame as wg  # noqa: E402
from nadare.backends.launcher import NikkeBackend  # noqa: E402
from nadare.backends.winegame_parts import launching  # noqa: E402
from nadare.core import gamesandbox, process, stopstate  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.core.events import Cancel, Cancelled, Event  # noqa: E402
from nadare.gui.workers import BackendController, BackendWorker  # noqa: E402


def _make_nikke(
    tmp_path: Path, extra_profile: dict | None = None
) -> tuple[NikkeBackend, list[Event]]:
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "NIKKE" / "nikke_launcher.exe", "MZ")
    wine = write_exe(tmp_path / "wine")
    profile = {"wine_binary": str(wine), "prefix": str(prefix)}
    if extra_profile:
        profile.update(extra_profile)
    backend = NikkeBackend(make_context(tmp_path, "launcher-nikke", profile))
    events: list[Event] = []
    backend.ctx.emit = events.append
    return backend, events


def _fake_clock(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    monkeypatch.setattr(launching.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(launching.time, "sleep", sleep)


def _patch_stop(
    monkeypatch: pytest.MonkeyPatch,
    alive: set[int],
    calls: list[tuple[str, list[int]]],
) -> None:
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


# ---------------------------------------------------------------------------
# Finding 1 (P1): process.run() used to stop draining line_q during before_stop(),
# deadlocking any chatty game in write() mid-shutdown.
# ---------------------------------------------------------------------------
def test_process_run_does_not_deadlock_a_chatty_child_during_before_stop(
    tmp_path: Path,
) -> None:
    close_req = tmp_path / "close_req"
    saved = tmp_path / "saved"
    got_term = tmp_path / "got_term"
    script = (
        "import pathlib, signal, sys, time\n"
        "def onterm(*_):\n"
        f"    pathlib.Path({str(got_term)!r}).write_text('term')\n"
        "    raise SystemExit(42)\n"
        "signal.signal(signal.SIGTERM, onterm)\n"
        f"while not pathlib.Path({str(close_req)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "# Emit realistic shutdown/DXVK/Wine log volume (>1024-line queue + 64 KiB pipe).\n"
        "line = 'X' * 120\n"
        "for i in range(3000):\n"
        "    print(f'shutdown {i} {line}', flush=True)\n"
        f"pathlib.Path({str(saved)!r}).write_text('ok')\n"
    )
    cancel = Cancel()

    def before_stop() -> None:
        close_req.write_text("close")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not saved.exists():
            time.sleep(0.02)

    def press_stop() -> None:
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

    assert saved.exists(), "chatty child deadlocked on stdout pipe during before_stop()"
    assert not got_term.exists(), "child was killed by SIGTERM instead of finishing polite exit"


# ---------------------------------------------------------------------------
# Finding 2 (P1): _ask_game_to_close() passes raw os.environ to the Wine/Proton
# taskkill helper, leaking host credentials and Python/loader env vars.
# ---------------------------------------------------------------------------
def test_polite_stop_helper_must_not_leak_host_secrets_or_python_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _make_nikke(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, {7}, calls)

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_super_secret")
    monkeypatch.setenv("NADARE_API_KEY", "nadare_super_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/opt/venv/lib/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/venv")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/preload.so")

    stopstate.save(
        backend.prefix(),
        kind="taskkill",
        commands=[["/runner/proton", "run", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={"WINEPREFIX": str(backend.prefix()), "LD_LIBRARY_PATH": "/deliberate/lib"},
    )

    spawned_envs: list[dict[str, str]] = []

    class FakeProc:
        pid = 4321

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_spawn(argv, *, env=None, **_kwargs):
        spawned_envs.append(dict(env or {}))
        return FakeProc()

    monkeypatch.setattr(process, "spawn", fake_spawn)
    monkeypatch.setattr(launching.os, "killpg", lambda *_a: None)

    backend.stop()

    assert spawned_envs, "taskkill helper was never spawned"
    assert spawned_envs[0]["LD_LIBRARY_PATH"] == "/deliberate/lib"
    assert spawned_envs[0]["WINEPREFIX"] == str(backend.prefix())
    leaked = [
        key
        for key in (
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "NADARE_API_KEY",
            "SSH_AUTH_SOCK",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "LD_PRELOAD",
        )
        if key in spawned_envs[0]
    ]
    assert not leaked, f"Wine/Proton taskkill helper inherited forbidden host env vars: {leaked}"


# ---------------------------------------------------------------------------
# Finding 3 (P1/P2): process.run() unconditionally SIGKILLs the process group
# even after the direct child exited cleanly during before_stop().
# ---------------------------------------------------------------------------
def test_process_run_does_not_sigkill_grandchild_after_clean_before_stop_exit(
    tmp_path: Path,
) -> None:
    close_req = tmp_path / "close_req"
    child_done = tmp_path / "child_done"
    flushed = tmp_path / "flushed"
    ready = tmp_path / "grandchild_ready"
    grandchild_script = (
        "import pathlib, signal, time\n"
        "# Stay in the same process group (like wineserver flushing registry after game exit).\n"
        "def onterm(*_):\n"
        "    time.sleep(0.2)\n"
        f"    pathlib.Path({str(flushed)!r}).write_text('flushed-on-term')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, onterm)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        f"while not pathlib.Path({str(child_done)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "time.sleep(0.3)\n"
        f"pathlib.Path({str(flushed)!r}).write_text('flushed-clean')\n"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])\n"
        f"while not pathlib.Path({str(close_req)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        f"pathlib.Path({str(child_done)!r}).write_text('done')\n"
    )
    cancel = Cancel()

    def before_stop() -> None:
        close_req.write_text("close")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not child_done.exists():
            time.sleep(0.01)
        time.sleep(0.05)  # allow direct child to exit cleanly

    def press_stop() -> None:
        deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.cancel()

    thread = threading.Thread(target=press_stop, daemon=True)
    thread.start()
    try:
        with pytest.raises(Cancelled):
            process.run(
                [sys.executable, "-c", parent_script],
                cancel=cancel,
                before_stop=before_stop,
            )
    finally:
        thread.join(timeout=2)

    assert ready.exists(), "grandchild never installed its shutdown handler"
    # A correct implementation may return while a politely closing descendant
    # is still flushing. Give that descendant time to produce its marker.
    deadline = time.monotonic() + 1.0
    while not flushed.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert flushed.exists(), (
        "grandchild in pgid was instantaneously SIGKILLed after direct child exited cleanly "
        "during before_stop() without ever getting SIGTERM or grace to flush"
    )


# ---------------------------------------------------------------------------
# Finding 4 in the Neon set (a second slow helper must keep the grace earned by
# the first) is not ported: it asks for grace after the helper cutoff, which is
# the branch's stated policy. See REPORT-codexfix.md.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Finding 5 (P2): Clicking Stop ONCE during launch runs polite stop TWICE
# (2x grace = 16s) when any process survives launch's process-group signal
# (e.g. Flatpak Sandbox where flatpak-spawn exits while game keeps running).
# ---------------------------------------------------------------------------
def test_single_stop_during_launch_must_not_wait_two_graces_when_queued_stop_runs(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend1, _ = _make_nikke(tmp_path)
    backend2, _ = _make_nikke(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, {7}, calls)
    stopstate.save(
        backend1.prefix(),
        kind="taskkill",
        commands=[["wine", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={},
    )

    helper_calls: list[list[str]] = []
    for b in (backend1, backend2):
        monkeypatch.setattr(
            b,
            "_run_stop_helper",
            lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
        )

    monkeypatch.setattr(BackendWorker, "start", lambda self: None)
    controller = BackendController(Config.load(tmp_path / "config.json"))
    launch = BackendWorker(controller._config, "p", "launch", parent=controller)
    controller._workers["p"] = launch
    monkeypatch.setattr(launch, "isRunning", lambda: True)
    try:
        # User clicks Stop ONCE while launch is running.
        assert controller.start("p", "stop")

        # Launch worker runs its before_stop hook (waits 1 full 8s grace: 100 -> 108).
        backend1.ctx.cancel = launch._cancel_token
        assert backend1._ask_game_to_close() == [7]
        assert clock[0] == 108.0

        # Launch worker finishes (e.g. flatpak-spawn exited, game PID 7 still alive),
        # so BackendController starts the queued stop worker on a fresh backend instance.
        monkeypatch.setattr(launch, "isRunning", lambda: False)
        controller._on_worker_finished("p", launch)
        queued = controller.worker("p")
        assert queued is not None and queued.operation == "stop"
        backend2.ctx.cancel = queued._cancel_token
        queued._dispatch(backend2)

        assert len(helper_calls) == 1, f"polite taskkill ran {len(helper_calls)} times for one Stop"
        assert clock[0] == 108.0, f"one Stop waited {clock[0] - 100.0}s (two full graces)"
    finally:
        controller._workers.clear()


# ---------------------------------------------------------------------------
# Finding 6 (P2): NikkeBackend._stop_tray_launcher() sets
# _polite_stop_attempted = True on self before the new game starts, silently
# disabling polite stop for the entire new session.
# ---------------------------------------------------------------------------
def test_nikke_tray_cleanup_before_play_must_not_disable_polite_stop_for_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _make_nikke(tmp_path, {"secure_mode": False})
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, {7}, calls)

    # Previous session left a saved stopstate and an idle launcher in the tray.
    stopstate.save(
        backend.prefix(),
        kind="taskkill",
        commands=[["wine", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={},
    )
    # _launcher_pids() is called once in launch() check, once in _stop_tray_launcher()
    # to collect pids, and then in the wait loop until empty.
    tray_checks = iter([[7], [7], []])
    monkeypatch.setattr(backend, "_launcher_pids", lambda: next(tray_checks, []))
    monkeypatch.setattr(backend, "_idle_in_tray", lambda: True)
    monkeypatch.setattr(backend, "_launcherless_wanted", lambda: False)
    monkeypatch.setattr(backend, "_hidden_watch", lambda: None)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda **_kw: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda **_kw: None)
    target = backend.find_launcher()
    monkeypatch.setattr(
        backend, "build_launch", lambda: (["wine", str(target)], {}, target)
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
        backend, "_game_process_watch", lambda: contextlib.nullcontext(threading.Event())
    )

    helper_calls: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
    )

    session_helper_count_before: list[int] = []

    def fake_stream(_argv, _env, *, before_stop=None, **_kwargs):
        assert before_stop is not None
        session_helper_count_before.append(len(helper_calls))
        # Player later clicks Stop during the new session.
        backend.ctx.cancel.cancel()
        before_stop()
        return 0

    monkeypatch.setattr(launching, "stream", fake_stream)

    backend.launch()

    assert session_helper_count_before, "new session never reached stream()"
    assert len(helper_calls) > session_helper_count_before[0], (
        "polite stop was silently skipped for the new NIKKE session because "
        "_stop_tray_launcher() left _polite_stop_attempted=True"
    )


# ---------------------------------------------------------------------------
# Finding 7 (P2): Stale unsandboxed taskkill stopstate is never cleared on
# normal exit or failed _record_polite_stop(), so Stop runs taskkill outside
# the Sandbox for a Sandboxed game.
# ---------------------------------------------------------------------------
def test_stale_unsandboxed_stopstate_must_not_run_taskkill_for_sandboxed_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _make_nikke(tmp_path, {"secure_mode": False})
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, {7}, calls)

    # Launch 1 ran unsandboxed and saved kind="taskkill", then exited normally.
    backend._record_polite_stop(["/usr/bin/wine", "nikke_launcher.exe"], {}, 1)
    assert stopstate.load(backend.prefix())["kind"] == "taskkill"

    # User switches Sandbox ON for Launch 2; _record_polite_stop fails to write
    # (caught with "Stop will end the game at once").
    backend.ctx.profile["secure_mode"] = True
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    monkeypatch.setattr(
        stopstate, "save", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("ENOSPC"))
    )
    backend._record_polite_stop(["/usr/bin/wine", "nikke_launcher.exe"], {}, 1)

    helper_calls: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda argv, _env, _deadline: helper_calls.append(list(argv)) or True,
    )

    backend.stop()

    assert not helper_calls, (
        "Stop executed a stale unsandboxed taskkill outside the Sandbox for a Sandboxed game"
    )


# ---------------------------------------------------------------------------
# Finding 8 (P2): When Sandbox is on by default and falls back to unsandboxed
# (missing/blocked bwrap), _record_polite_stop() still records kind="x11"
# instead of kind="taskkill", breaking polite stop on native Wayland.
# ---------------------------------------------------------------------------
def test_sandbox_fallback_to_unsandboxed_must_record_taskkill_not_x11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Restore production default (conftest sets DEFAULT_ON=False globally for tests).
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)
    # Default profile: secure_mode unset -> sandbox_default_on=True, not insisted.
    backend, _ = _make_nikke(tmp_path)
    assert backend.secure_mode_enabled() is True
    assert backend._sandbox_insisted() is False

    # Host has no bubblewrap, so _secure_wrap falls back to running unsandboxed.
    monkeypatch.setattr(launching.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(
        gamesandbox,
        "locate",
        lambda: gamesandbox.Located(binary=None, found=False, why="not found"),
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda **_kw: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda **_kw: None)
    monkeypatch.setattr(backend, "_launcherless_wanted", lambda: False)
    monkeypatch.setattr(backend, "_hidden_watch", lambda: None)
    monkeypatch.setattr(backend, "_launcher_pids", lambda: [])
    monkeypatch.setattr(backend, "_runtime_note", lambda: "")
    monkeypatch.setattr(backend, "_log_launch_details", lambda _env: None)
    monkeypatch.setattr(backend, "_begin_game_output", lambda: None)
    monkeypatch.setattr(backend, "_end_game_output", lambda: None)
    monkeypatch.setattr(backend, "_stop_link_relay", lambda: None)
    probe = SimpleNamespace(result="", stop=lambda: None, join=lambda **_kw: None)
    probe.start = lambda: probe
    monkeypatch.setattr(backend, "_renderer_probe", lambda: probe)
    monkeypatch.setattr(
        backend, "_game_process_watch", lambda: contextlib.nullcontext(threading.Event())
    )

    launched_argv: list[list[str]] = []
    monkeypatch.setattr(
        launching, "stream", lambda argv, *_a, **_kw: launched_argv.append(list(argv)) or 0
    )

    assert backend.launch() == 0
    assert launched_argv and "bwrap" not in Path(launched_argv[0][0]).name
    saved = stopstate.load(backend.prefix())
    assert saved is not None and saved["kind"] == "taskkill", (
        f"game ran unsandboxed (fallback), yet stopstate recorded kind={saved!r}"
    )


def test_failed_invalidation_rejects_old_generation_in_fresh_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither clearing nor writing works: the surviving request is still not this launch's.

    The queued Stop worker is a fresh backend, so only the saved request's identity can
    tell it that the file belongs to the launch before this one (#1421 review).
    """
    backend, _ = _make_nikke(tmp_path, {"secure_mode": False})
    backend._record_polite_stop(["wine", "nikke_launcher.exe"], {}, 1)
    assert stopstate.load(backend.prefix()) is not None
    monkeypatch.setattr(stopstate, "clear", lambda *_a: None)
    monkeypatch.setattr(
        stopstate, "save", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("ENOSPC"))
    )
    backend._record_polite_stop(["wine", "nikke_launcher.exe"], {}, 1)
    other, _ = _make_nikke(tmp_path, {"secure_mode": False})
    _fake_clock(monkeypatch, [100.0])
    _patch_stop(monkeypatch, {7}, [])
    monkeypatch.setattr(
        other, "_run_stop_helper", lambda *_a: pytest.fail("old generation was accepted")
    )
    other.stop()
