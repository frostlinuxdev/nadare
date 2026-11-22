# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""One log per Move (#1352): from, to, how, and how it ended.

An instant move on the same drive used to leave no trace at all. The log sits with the game's
other run logs (``<logs>/<game-id>/move-<stamp>.log``), so the log viewer and support reports
find it. Writing a log must never break a move: every failure here is swallowed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import __version__
from . import paths, runlog


def write(
    profile: str,
    *,
    title: str,
    src: Path,
    dst: Path,
    how: str,
    outcome: str,
    logs: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Write a new move log and return its path, or None when it could not be written."""
    stamp = now or datetime.now()
    try:
        path = runlog.run_log_path(logs or paths.logs_dir(), profile, "move", now=stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        when = stamp.isoformat(timespec="seconds")
        lines = [
            f"===== {when} move ({title}) | nadare {__version__} =====",
            f"from: {src}",
            f"to: {dst}",
        ]
        if how:
            lines.append(f"how: {how}")
        lines.append(f"[end] {outcome}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        return None
    return path


def append(path: Path | None, line: str) -> None:
    """Add a later line (the original's removal) to a move log; never raises."""
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


__all__ = ["append", "write"]
