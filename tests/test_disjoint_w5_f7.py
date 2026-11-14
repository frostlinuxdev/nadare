# SPDX-License-Identifier: GPL-3.0-or-later
"""W5-F7: the disk-space promise is a per-process dict, so two processes both fit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import diskspace
from nadare.core.backends import BackendError

DEVICE = 424242


def _child_code() -> str:
    return f"""
import sys, time
from pathlib import Path
from nadare.core import diskspace
diskspace.free_bytes = lambda path: 1000
diskspace._device = lambda path: {DEVICE}
diskspace.reserve(Path(sys.argv[1]), 800, owner="first-process")
print("reserved", flush=True)
time.sleep(60)
"""


def test_a_second_process_sees_the_first_process_disk_reservation(tmp_path, monkeypatch):
    # Both processes see a 1000-byte filesystem; the first promises 800 bytes.
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    monkeypatch.setattr(diskspace, "_device", lambda path: DEVICE)

    env = dict(os.environ)
    src = Path(diskspace.__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH")]))
    child = subprocess.Popen(
        [sys.executable, "-c", _child_code(), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = child.stdout.readline().strip()  # child prints "reserved" once holding the promise
        assert line == "reserved", child.stderr.read()
        # The first process still promises 800 of the 1000 bytes.  A second
        # 800-byte install cannot also fit, in this process or any other.
        with pytest.raises(BackendError):
            diskspace.reserve(tmp_path, 800, owner="second-process")
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


def test_two_games_on_one_filesystem_see_each_others_reservation(tmp_path, monkeypatch):
    """Two processes installing two different games onto one disk (the real case)."""
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    monkeypatch.setattr(diskspace, "_device", lambda path: DEVICE)
    first, second = tmp_path / "game-one", tmp_path / "game-two"
    first.mkdir()
    second.mkdir()
    env = dict(os.environ)
    src = Path(diskspace.__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH")]))
    child = subprocess.Popen(
        [sys.executable, "-c", _child_code(), str(first)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout.readline().strip() == "reserved", child.stderr.read()
        with pytest.raises(BackendError):
            diskspace.reserve(second, 800, owner="second-process")
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
