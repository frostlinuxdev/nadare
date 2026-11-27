# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-test deleteLater flush must not destroy a running QThread (CI 3.13 abort, 2026-11-26)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent

SCRIPT = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QObject, QThread
from PyQt6.QtWidgets import QApplication
import conftest

app = QApplication([])

class Slow(QThread):
    def run(self):
        time.sleep(0.5)

owner = QObject()
thread = Slow(owner)
thread.start()
owner.deleteLater()           # a test tears down the window while its worker still runs
conftest.flush_deferred_deletes()
print("survived")
"""


def test_the_flush_waits_for_a_running_thread_before_deleting_its_owner():
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    run = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(TESTS)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert run.returncode == 0, run.stderr[-800:]
    assert "survived" in run.stdout


# A Store page deleted by the flush while its art workers (QThreadPool, not QThread) still
# ran: a worker emitted on the page's signals object as it died (CI 3.12 segfault, #1863).
POOL_SCRIPT = r"""
import sys, threading, time
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QApplication
import conftest

app = QApplication([])

class Signals(QObject):
    finished = pyqtSignal()

done = []
release = threading.Event()

class Fetch(QRunnable):
    def __init__(self, signals, block):
        super().__init__()
        self.signals, self.block = signals, block
    def run(self):
        if self.block:
            release.wait(30)
        else:
            time.sleep(0.5)
        done.append(1)
        self.signals.finished.emit()

mode = sys.argv[2]
owner = QObject()
signals = Signals(owner)            # the page owns the signals, as StorePage does
QThreadPool.globalInstance().start(Fetch(signals, block=(mode == "stuck")))
owner.deleteLater()
if mode == "wait":
    conftest.flush_deferred_deletes()
    print("finished-before-delete" if done else "deleted-while-running")
else:
    from PyQt6 import sip
    conftest.flush_deferred_deletes(timeout_ms=200)
    print("kept" if not sip.isdeleted(owner) else "deleted")
    release.set()
    QThreadPool.globalInstance().waitForDone(10_000)
"""


def _pool_run(mode: str) -> str:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    run = subprocess.run(
        [sys.executable, "-c", POOL_SCRIPT, str(TESTS), mode],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert run.returncode == 0, run.stderr[-800:]
    return run.stdout


def test_the_flush_lets_pool_workers_finish_before_deleting_what_they_emit_on():
    assert "finished-before-delete" in _pool_run("wait")


def test_a_pool_worker_that_will_not_finish_keeps_the_deletes_pending():
    assert "kept" in _pool_run("stuck")


# The GUI smoke modules have their own between-tests cleanup; it flushed deletes without waiting
# for the pool and segfaulted CI 3.13 on #1865 (images.py worker emitting on a deleted page).
SMOKE_SCRIPT = POOL_SCRIPT.replace(
    "conftest.flush_deferred_deletes()\n",
    "import _gui_smoke_helpers\n    _gui_smoke_helpers.collect_garbage()\n",
)


def test_the_gui_smoke_cleanup_also_waits_for_pool_workers():
    assert "collect_garbage" in SMOKE_SCRIPT
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    run = subprocess.run(
        [sys.executable, "-c", SMOKE_SCRIPT, str(TESTS), "wait"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert run.returncode == 0, run.stderr[-800:]
    assert "finished-before-delete" in run.stdout


# The flush ran after every test and walked gc.get_objects() each time to find running
# QThreads: ~23 ms a call on a 250k-object heap, a third of the run, and GitHub CI 3.13 hit
# its 25-minute limit on b27-rc1 (b26: 7 min). After the first call it must track threads
# as they start instead of scanning the heap.
LATE_SCRIPT = r"""
import gc, sys, time
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QObject, QThread
from PyQt6.QtWidgets import QApplication
import conftest

app = QApplication([])
conftest.flush_deferred_deletes()   # the first test's flush

scans = []
real_get_objects = gc.get_objects
def counting(*args, **kwargs):
    scans.append(1)
    return real_get_objects(*args, **kwargs)
gc.get_objects = counting

class Slow(QThread):
    def run(self):
        time.sleep(0.5)

owner = QObject()
thread = Slow(owner)
thread.start()                      # started after the first flush
owner.deleteLater()
conftest.flush_deferred_deletes()
for _ in range(50):
    conftest.flush_deferred_deletes()
print("survived", "scans=%d" % len(scans))
"""


def _late_run() -> str:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    run = subprocess.run(
        [sys.executable, "-c", LATE_SCRIPT, str(TESTS)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert run.returncode == 0, run.stderr[-800:]
    return run.stdout


def test_the_flush_waits_for_a_thread_started_after_its_first_call():
    assert "survived" in _late_run()


def test_the_flush_does_not_walk_the_heap_after_its_first_call():
    assert "scans=0" in _late_run()
