# SPDX-License-Identifier: GPL-3.0-or-later
"""An exception escaping ``process.run`` must not orphan its child process."""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from nadare.core import process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same-user children only
        return True
    return True


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
