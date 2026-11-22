# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Stop a launcher once its window has stayed hidden long enough.

The watch only fires after the window has been seen at least once. While it is shown, or
while anything else runs in the prefix, the hidden timer restarts; an unknown answer (native
Wayland, no X11 library) never stops anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

__all__ = ["HiddenWindowWatch", "WriteActivity"]


class HiddenWindowWatch:
    """Poll ``shown``/``busy`` and call ``on_idle`` once after a long hidden stretch."""

    def __init__(
        self,
        shown: Callable[[], bool | None],
        busy: Callable[[], bool],
        on_idle: Callable[[], None],
        *,
        idle_seconds: float = 60.0,
        poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.shown = shown
        self.busy = busy
        self.on_idle = on_idle
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.fired = False
        self._seen = False
        self._hidden_since: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def step(self) -> bool:
        """One poll; True when this step fired ``on_idle``."""
        if self.fired:
            return False
        try:
            shown = self.shown()
        except Exception:
            shown = None
        if shown is True:
            self._seen = True
            self._hidden_since = None
            return False
        # Only a real False counts as hidden, and only after the window was seen once.
        if shown is not False or not self._seen:
            self._hidden_since = None
            return False
        try:
            busy = self.busy()
        except Exception:
            busy = True
        if busy:
            self._hidden_since = None
            return False
        now = self.clock()
        if self._hidden_since is None:
            self._hidden_since = now
            return False
        if now - self._hidden_since < self.idle_seconds:
            return False
        self.fired = True
        try:
            self.on_idle()
        except Exception:
            pass
        return True

    def start(self) -> None:
        """Poll in a daemon thread until stopped or fired."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            if self.step():
                return

    def stop(self) -> None:
        """Ask the thread to end and wait briefly for it."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)


#: Bytes a launcher must write between two looks to count as updating (logs and cookies stay
#: far below it; a download writes tens of MB per second).
UPDATE_WRITE_BYTES = 8 * 1024**2


def _write_bytes(pid: int) -> int | None:
    """``write_bytes`` from ``/proc/<pid>/io``, or ``None`` when it cannot be read."""
    try:
        with open(f"/proc/{pid}/io", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("write_bytes:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


class WriteActivity:
    """Whether processes wrote a lot to disk since the last look (an update downloading).

    A launcher that updates itself does it inside its own process, so its name looks idle;
    its disk writes do not. The first look at a process is its baseline. An unreadable
    process (another user, a namespace) is ignored, never busy forever.
    """

    def __init__(
        self,
        reader: Callable[[int], int | None] = _write_bytes,
        threshold: int = UPDATE_WRITE_BYTES,
    ) -> None:
        self.reader = reader
        self.threshold = threshold
        self._last: dict[int, int] = {}

    def active(self, pids: Iterable[int]) -> bool:
        busy = False
        seen: dict[int, int] = {}
        for pid in pids:
            value = self.reader(pid)
            if value is None:
                continue
            seen[pid] = value
            before = self._last.get(pid)
            if before is not None and value - before >= self.threshold:
                busy = True
        self._last = seen
        return busy

