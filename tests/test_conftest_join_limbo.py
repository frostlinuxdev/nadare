# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-test background-thread wait survives a thread that is still starting (CI flake).

``threading.enumerate()`` lists a thread between ``start()`` and its first instruction, and
``join()`` on it raises RuntimeError. That surfaced as "ERROR at teardown ... cannot join
thread before it is started" in test_selfupdate_window on CI.
"""

from __future__ import annotations

import threading
import time

from conftest import _join_until


class _StartsLate(threading.Thread):
    """A thread whose first joins fail the way a just-started one does."""

    def __init__(self) -> None:
        super().__init__(target=lambda: None, daemon=True)
        self.refused = 0

    def join(self, timeout=None):  # noqa: ANN001 - matches threading.Thread
        if self.refused < 3:
            self.refused += 1
            raise RuntimeError("cannot join thread before it is started")
        return super().join(timeout)


def test_a_starting_thread_is_waited_for_not_an_error():
    thread = _StartsLate()
    thread.start()
    _join_until(thread, time.monotonic() + 2.0)
    assert thread.refused == 3
    assert not thread.is_alive()


def test_a_thread_that_never_starts_gives_up_at_the_deadline():
    thread = threading.Thread(target=lambda: None)  # never started: join always raises
    began = time.monotonic()
    _join_until(thread, began + 0.1)
    assert 0.05 <= time.monotonic() - began < 1.0
