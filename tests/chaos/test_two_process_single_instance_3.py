# SPDX-License-Identifier: GPL-3.0-or-later
"""Two simultaneous starts over a stale socket must leave exactly one primary."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only without PyQt6
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

import nadare

_CHILD = r"""
import sys, time
from pathlib import Path
from PyQt6.QtWidgets import QApplication

app = QApplication([])
from nadare.gui import single_instance as si

name, mode, sync = sys.argv[1], sys.argv[2], Path(sys.argv[3])
if mode == "b":
    original = si.QLocalServer

    class PausedServer(original):
        @staticmethod
        def removeServer(n):
            (sync / "b-before-remove").write_text("1")
            deadline = time.monotonic() + 10
            while not (sync / "b-go").exists():
                if time.monotonic() > deadline:
                    raise SystemExit("parent did not signal")
                time.sleep(0.01)
            return original.removeServer(n)

    si.QLocalServer = PausedServer

instance = si.SingleInstance(name)
started = instance.start()
(sync / f"{mode}-started").write_text(str(started))
deadline = time.monotonic() + 10
while not (sync / "stop").exists():
    if time.monotonic() > deadline:
        break
    time.sleep(0.01)
instance.close()
"""


def test_two_simultaneous_starts_on_a_stale_socket_pick_one_primary(tmp_path):
    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"

    name = str(tmp_path / "s.sock")
    # A stale Unix socket: bound once, then its owner died without unlinking it.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(name)
    stale.close()

    sync = tmp_path / "sync"
    sync.mkdir()

    def spawn(mode: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", _CHILD, name, mode, str(sync)], env=env
        )

    def wait_for(path: Path, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                raise AssertionError(f"child did not create {path.name}")
            time.sleep(0.01)

    second = spawn("b")
    first = None
    try:
        wait_for(sync / "b-before-remove")
        first = spawn("a")
        wait_for(sync / "a-started")
        # B was stopped right before reclaiming the stale socket; A has now reclaimed it
        # and is listening. B resumes and unconditionally removes the path.
        (sync / "b-go").write_text("go")
        wait_for(sync / "b-started")
        first_started = (sync / "a-started").read_text()
        second_started = (sync / "b-started").read_text()
        print("first process start():", first_started)
        print("second process start():", second_started)
        assert sorted([first_started, second_started]) == ["False", "True"], (
            "both processes became primary (or neither did) after a stale-socket start"
        )
    finally:
        (sync / "stop").write_text("stop")
        for child in (first, second):
            if child is None:
                continue
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
