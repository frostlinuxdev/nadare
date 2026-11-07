# SPDX-License-Identifier: GPL-3.0-or-later
"""An exception escaping ``process.run`` must not orphan its child process."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from nadare.core import process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same-user children only
        return True
    # A killed orphan stays a zombie until its parent reaps it, and a container without an init
    # process (the CI runner) never does: a zombie is gone as far as the test is concerned.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.01)
    return not _alive(pid)


def _kill_quietly(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _interrupt_on_first_line(marks: dict[str, int]) -> process.Emitter:
    def emit(event: process.Event) -> None:
        if event.kind != "log":
            return
        marks["pid"] = int(event.message)
        raise KeyboardInterrupt

    return emit


def test_interrupt_terminates_child_and_preserves_exception() -> None:
    marks: dict[str, int] = {}
    script = "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n"
    try:
        with pytest.raises(KeyboardInterrupt):
            process.run(
                [sys.executable, "-c", script],
                emit=_interrupt_on_first_line(marks),
            )
        assert _wait_dead(marks["pid"], 5), "interrupted run left its child alive"
    finally:
        _kill_quietly(marks.get("pid"))


def test_interrupt_kills_child_that_ignores_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "GRACE_SECONDS", 0.3)
    marks: dict[str, int] = {}
    script = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            process.run(
                [sys.executable, "-c", script],
                emit=_interrupt_on_first_line(marks),
            )
        elapsed = time.monotonic() - started
        assert _wait_dead(marks["pid"], process.GRACE_SECONDS + 5), (
            "child that ignored SIGTERM survived the interrupt"
        )
        assert elapsed < process.GRACE_SECONDS + 5, "interrupt cleanup was not bounded"
    finally:
        _kill_quietly(marks.get("pid"))


def test_a_further_interrupt_during_cleanup_still_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third Ctrl-C lands while run() waits out the SIGTERM grace period."""
    import subprocess

    monkeypatch.setattr(process, "GRACE_SECONDS", 0.5)
    real_wait = subprocess.Popen.wait
    calls = {"n": 0}

    def wait(self, timeout=None):
        if timeout == process.GRACE_SECONDS:
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    marks: dict[str, int] = {}
    script = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            process.run([sys.executable, "-c", script], emit=_interrupt_on_first_line(marks))
        assert _wait_dead(marks["pid"], 5), "a second interrupt left the child alive"
    finally:
        _kill_quietly(marks.get("pid"))


def test_a_raising_emitter_after_the_leader_exited_still_stops_its_descendants(tmp_path) -> None:
    """The leader is gone but a grandchild is not: the group is cleaned up anyway (#896)."""
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > {pidfile}; echo hello; exit 0"
    seen: list[str] = []

    def emit(event) -> None:
        if event.kind == "log" and event.message == "hello":
            time.sleep(0.6)  # let the leader exit while its output is still being forwarded
            seen.append("raising")
            raise RuntimeError("the log sink is broken")

    grandchild = 0
    try:
        with pytest.raises(RuntimeError, match="log sink"):
            process.run(["sh", "-c", script], env=dict(os.environ), emit=emit)
        assert seen == ["raising"]
        grandchild = int(pidfile.read_text())
        assert _wait_dead(grandchild, 8.0), "the grandchild was left running"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
