# SPDX-License-Identifier: GPL-3.0-or-later
"""Kill only a disposable child after it acknowledges the requested checkpoint."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

CHECKPOINT = "print('CRASH_READY', flush=True); __import__('time').sleep(60)"


def kill_at_checkpoint(script, cwd):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root / "tests")])
    env["QT_QPA_PLATFORM"] = "offscreen"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "child never reached checkpoint"
        line = child.stdout.readline().strip()
        if line != "CRASH_READY":
            child.kill()
            _, errors = child.communicate(timeout=5)
            raise AssertionError(f"checkpoint missing: {line!r}; {errors}")
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
