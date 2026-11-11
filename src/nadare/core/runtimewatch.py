# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Recognise a blocked or failed runtime from a child's output and explain it.

These failures look like ordinary non-zero exits (or, for umu, even a zero
exit), so a backend has to remember what it saw while streaming the child's
output before it can make the final error message actionable. :class:`RuntimeWatch`
keeps those two flags and decorates the message on demand.
"""

from __future__ import annotations

import re

from . import umu, userns

__all__ = ["NO_OPENGL_HINT", "RuntimeWatch"]

#: The game's own GLFW message when the Windows side gets no OpenGL from the host (#132).
_NO_OPENGL = re.compile(r"WGL: The driver does not appear to support OpenGL", re.IGNORECASE)

NO_OPENGL_HINT = (
    "The game could not get OpenGL from your system. Run `nadare doctor`: it names missing GL "
    "libraries (usually your distribution's libglvnd / mesa packages). If umu-run is not "
    "installed, installing umu-launcher fixes this on most systems."
)


class RuntimeWatch:
    """Accumulate runtime failure flags from output lines, then explain them."""

    userns_blocked: bool = False
    umu_setup_failed: bool = False
    umu_runtime_mismatch: bool = False
    no_opengl: bool = False

    def __init__(self) -> None:
        self.userns_blocked = False
        self.umu_setup_failed = False
        self.umu_runtime_mismatch = False
        self.no_opengl = False

    def feed(self, line: str) -> None:
        """Record any failure that ``line`` reveals. Never raises."""
        try:
            if userns.is_failure_line(line):
                self.userns_blocked = True
            if umu.is_setup_failure_line(line):
                self.umu_setup_failed = True
            if umu.is_runtime_mismatch_line(line):
                self.umu_runtime_mismatch = True
            if _NO_OPENGL.search(line):
                self.no_opengl = True
        except Exception:  # pragma: no cover - defensive: output must never break a run
            pass

    def explain(self, message: str) -> str:
        """Return ``message`` plus the hint for each failure that was seen."""
        out = userns.with_hint(message, blocked=self.userns_blocked)
        if self.umu_setup_failed:
            out = out + "\n" + umu.SETUP_HINT
        if self.umu_runtime_mismatch:
            out = out + "\n" + umu.RUNTIME_MISMATCH_HINT
        if self.no_opengl:
            out = out + "\n" + NO_OPENGL_HINT
        return out
