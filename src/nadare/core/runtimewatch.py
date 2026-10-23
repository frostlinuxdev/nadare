# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Recognise a blocked or failed runtime from a child's output and explain it.

Both failures look like ordinary non-zero exits (or, for umu, even a zero
exit), so a backend has to remember what it saw while streaming the child's
output before it can make the final error message actionable. :class:`RuntimeWatch`
keeps those two flags and decorates the message on demand.
"""

from __future__ import annotations

from . import umu, userns

__all__ = ["RuntimeWatch"]


class RuntimeWatch:
    """Accumulate runtime failure flags from output lines, then explain them."""

    userns_blocked: bool = False
    umu_setup_failed: bool = False

    def __init__(self) -> None:
        self.userns_blocked = False
        self.umu_setup_failed = False

    def feed(self, line: str) -> None:
        """Record any failure that ``line`` reveals. Never raises."""
        try:
            if userns.is_failure_line(line):
                self.userns_blocked = True
            if umu.is_setup_failure_line(line):
                self.umu_setup_failed = True
        except Exception:  # pragma: no cover - defensive: output must never break a run
            pass

    def explain(self, message: str) -> str:
        """Return ``message`` plus the hint for each failure that was seen."""
        out = userns.with_hint(message, blocked=self.userns_blocked)
        if self.umu_setup_failed:
            out = out + "\n" + umu.SETUP_HINT
        return out
