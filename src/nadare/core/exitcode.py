# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""How a game process ended, shared by the run log and the game page (#1126).

A report usually contains only the log bundle, so the log must name a crash the same way the
dialog does. Both read the classification here.
"""

from __future__ import annotations

#: Exit codes from being stopped by a signal (SIGINT, SIGKILL, SIGTERM), shell and raw form.
#: A crash signal (SIGSEGV, SIGABRT...) is not in here: that is a failed launch.
STOPPED_EXIT_CODES = frozenset({130, 137, 143, -2, -9, -15})

#: Crash signals (raw negative code and the shell's 128 + n): reported whatever the play time.
CRASH_SIGNALS = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 11: "SIGSEGV"}


def crash_signal(exit_code: int) -> str:
    """The crash signal's name when ``exit_code`` is one ("" otherwise)."""
    number = -exit_code if exit_code < 0 else exit_code - 128 if exit_code > 128 else 0
    return CRASH_SIGNALS.get(number, "")


def play_time(seconds: float) -> str:
    """A duration for people: ``37 seconds``, ``4 minutes``, ``2 hours``."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)} seconds"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes // 60} hours"


def short_duration(seconds: float) -> str:
    """A compact duration for the log's one-line ending: ``37s``, ``4m12s``, ``1h02m03s``."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
