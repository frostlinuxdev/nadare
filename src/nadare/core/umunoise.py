# SPDX-License-Identifier: GPL-3.0-or-later
"""Fold umu-run's harmless Stop traceback into one line of the launch log (#675).

On Stop, umu-run's SIGTERM handler (``umu_run.signal_handler``) calls ``os.kill`` on its whole
process tree. A child that already exited raises ``ProcessLookupError: [Errno 3] No such
process``, which umu logs with ``log.exception``: an ``ERROR`` line plus a full traceback. That
upstream noise landed in the user's log and in "Report a problem" and looked like a crash.

Only that exact block is folded: a traceback that runs through umu's ``signal_handler`` and
ends in ``ProcessLookupError``. Any other traceback, and an ``[Errno 3]`` line that is not
followed by one, passes through unchanged. Lines are held only while a traceback is open, at
most :data:`MAX_HELD` of them, and :meth:`StopNoiseFilter.flush` returns whatever is held.
"""

from __future__ import annotations

import re

NOTE = "umu-run: a game process had already exited when it was stopped (harmless, upstream)."
MAX_HELD = 80

_ERRNO_LINE = re.compile(r"\[Errno 3\] No such process\s*$")
_TRACEBACK = "Traceback (most recent call last):"
_END = re.compile(r"^ProcessLookupError: \[Errno 3\] No such process\s*$")


def _is_frame(line: str) -> bool:
    return line.startswith("  ")


_UMU_HANDLER_FRAME = re.compile(r'^\s*File "[^"]*umu_run\.py", line \d+, in signal_handler\s*$')


class StopNoiseFilter:
    """Feed it lines; it returns the lines to log (usually the same line)."""

    def __init__(self) -> None:
        self._held: list[str] = []
        self._in_traceback = False

    def feed(self, line: str) -> list[str]:
        if not self._held:
            if line.startswith(_TRACEBACK):
                self._held = [line]
                self._in_traceback = True
                return []
            if _ERRNO_LINE.search(line):
                self._held = [line]
                self._in_traceback = False
                return []
            return [line]
        if not self._in_traceback:
            # Held an [Errno 3] line: only a traceback right after it keeps it held.
            if line.startswith(_TRACEBACK):
                self._held.append(line)
                self._in_traceback = True
                return []
            return self.flush() + self.feed(line)
        if _is_frame(line):
            self._held.append(line)
            if len(self._held) > MAX_HELD:
                return self.flush()
            return []
        if _END.match(line):
            block = self._held + [line]
            self._held, self._in_traceback = [], False
            # Only a real frame line of umu's handler counts, not a line that merely names it
            # (#1207 b123).
            if any(_UMU_HANDLER_FRAME.match(item) for item in block):
                return [NOTE]
            return block
        # The traceback ended some other way: give everything back, then this line.
        return self.flush() + self.feed(line)

    def flush(self) -> list[str]:
        held, self._held, self._in_traceback = self._held, [], False
        return held


__all__ = ["MAX_HELD", "NOTE", "StopNoiseFilter"]
