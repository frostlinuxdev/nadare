"""guard() must not release the run lock before on_stop has finished."""
from __future__ import annotations

import threading
import time

from nadare.core import runlock


def test_guard_releases_the_lock_only_after_the_stop_callback_finishes(tmp_path):
    prefix = tmp_path / "prefix"
    entered = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def on_stop():
        entered.set()
        release.wait(10)
        finished.set()

    def release_later():
        time.sleep(2.0)  # longer than guard's 1 s join, shorter than the test budget
        release.set()

    threading.Thread(target=release_later, daemon=True).start()
    with runlock.guard(prefix, on_stop):
        runlock.request_stop(prefix)
        assert entered.wait(5.0), "the watcher never called on_stop"
    assert finished.is_set(), "the lock was released while on_stop was still running"
