# SPDX-License-Identifier: GPL-3.0-or-later
"""Disk-space reservations know their owner across PID namespaces (#1473).

Every ``flatpak run`` (the window, a CLI command, a shortcut) gets its own PID namespace but
shares Nadare's state folder, so two processes can both be "pid 2". A record holding the other
process's pid was taken for this process's own (and skipped), and a pid from another namespace
looked dead. A record now names a random per-process instance whose liveness is a held flock,
which every namespace on the same filesystem sees.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import diskspace
from nadare.core.backends import BackendError

SRC = Path(diskspace.__file__).resolve().parents[2]

_HOLDER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nadare.core import diskspace
diskspace.free_bytes = lambda path: 1000
r = diskspace.reserve(Path(sys.argv[2]), 700, owner="game-a", margin=0)
print("HELD", flush=True)
sys.stdin.readline()
"""


@pytest.fixture
def holder(tmp_path, monkeypatch):
    """Another process that holds a 700-byte promise on a 1000-byte disk."""
    target = tmp_path / "lib"
    target.mkdir()
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(SRC), str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "HELD", proc.stderr.read()
    yield proc, target
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _record_file(target: Path) -> Path:
    return diskspace._reservation_file(target)


def test_another_process_with_the_same_pid_still_counts(holder, monkeypatch):
    proc, target = holder
    monkeypatch.setattr(diskspace.os, "getpid", lambda: proc.pid)  # a colliding namespace pid
    with pytest.raises(BackendError, match="both installs"):
        diskspace.reserve(target, 700, owner="game-b", margin=0)


def test_a_pid_this_namespace_cannot_see_still_counts_while_its_lease_is_held(holder):
    proc, target = holder
    path = _record_file(target)
    records = json.loads(path.read_text())
    for record in records:
        record["pid"] = 2**22 + 12345  # not a process here, like a pid in another namespace
    path.write_text(json.dumps(records))
    with pytest.raises(BackendError, match="both installs"):
        diskspace.reserve(target, 700, owner="game-b", margin=0)


def test_an_owner_that_died_no_longer_counts(holder):
    proc, target = holder
    proc.kill()
    proc.wait()
    with diskspace.reserve(target, 700, owner="game-b", margin=0):
        pass


def test_a_released_promise_no_longer_counts(holder):
    proc, target = holder
    proc.stdin.write("\n")
    proc.stdin.flush()
    proc.wait(timeout=30)
    with diskspace.reserve(target, 700, owner="game-b", margin=0):
        pass


def test_a_record_without_an_instance_falls_back_to_its_pid(tmp_path, monkeypatch):
    """Records written by 0.1.0b24 name only a pid."""
    target = tmp_path / "lib"
    target.mkdir()
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    path = _record_file(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    live = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"],
                            stdin=subprocess.PIPE, text=True)
    try:
        legacy = {"owner": "old", "device": diskspace._device(target), "amount": 700,
                  "free_at_start": 1000, "path": str(target), "token": "t"}
        path.write_text(json.dumps([{**legacy, "pid": live.pid}]))
        with pytest.raises(BackendError):
            diskspace.reserve(target, 700, owner="game-b", margin=0)
        path.write_text(json.dumps([{**legacy, "pid": 2**22 + 12345}]))
        with diskspace.reserve(target, 700, owner="game-b", margin=0):
            pass
    finally:
        live.kill()
        live.wait()


def test_this_process_never_counts_its_own_record_twice(tmp_path, monkeypatch):
    target = tmp_path / "lib"
    target.mkdir()
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    with diskspace.reserve(target, 400, owner="game-a", margin=0):
        # Counted once (in memory): 1000 - 400 >= 500, so a second owner still fits.
        with diskspace.reserve(target, 500, owner="game-b", margin=0):
            pass


def test_a_record_whose_lease_file_is_gone_is_dead(tmp_path, monkeypatch):
    """An instance writes a record only while it holds its lease file: no file, no owner."""
    target = tmp_path / "lib"
    target.mkdir()
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1000)
    path = _record_file(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"instance": "f" * 32, "pid": 1, "owner": "gone", "amount": 700,
              "device": diskspace._device(target), "free_at_start": 1000,
              "path": str(target), "token": "t"}
    path.write_text(json.dumps([record]))  # pid 1 is alive, but the instance is what counts
    with diskspace.reserve(target, 700, owner="game-b", margin=0):
        pass
