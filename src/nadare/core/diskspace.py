# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Disk-space checks for downloads and installs.

Every download that knows its size up front can call :func:`check_space` before
opening a socket; the user gets one clear error instead of a half-written tree
when the target filesystem fills up. The check is advisory and racy by nature
(another process may claim the bytes first), but it catches the common case of
picking a tiny partition in the Add game wizard.
"""

from __future__ import annotations

import os
from pathlib import Path

from .backends import BackendError

#: Fraction of headroom added on top of ``needed_bytes``.
DEFAULT_MARGIN = 0.05


def _existing(path: str | os.PathLike[str]) -> Path:
    """The nearest existing ancestor of ``path`` (or the filesystem root)."""
    candidate = Path(path).expanduser()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def free_bytes(path: str | os.PathLike[str]) -> int:
    """Free bytes available to the user on the filesystem holding ``path``.

    Raises :class:`BackendError` (not a raw ``OSError``) when the filesystem
    cannot be queried, e.g. the ancestor chosen by :func:`_existing` vanished
    between the ``exists`` check and ``statvfs``.
    """
    candidate = _existing(path)
    try:
        stat = os.statvfs(candidate)
    except OSError as exc:
        raise BackendError(f"cannot read free space for {candidate}: {exc}") from exc
    return int(stat.f_bavail) * int(stat.f_frsize)


def mount_point(path: str | os.PathLike[str]) -> str:
    """The mount point of the filesystem holding ``path``."""
    candidate = _existing(path)
    while not os.path.ismount(candidate):
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return str(candidate)


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _three_digits(value: float) -> str:
    """``value`` with at most three significant digits and no trailing ``.0``."""
    text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
    return text[:-2] if text.endswith(".0") else text


def human_size(size: float) -> str:
    """The one byte-size format of the app: ``512 B``, ``1.5 KiB``, ``13 MiB``, ``1.5 GiB``.

    Binary units, labelled as such; three significant digits; a size that rounds up to 1024
    moves to the next unit (``1 MiB``, never ``1024 KiB``).
    """
    value = float(max(0, size))
    if value < 1024:
        return f"{int(value)} B"
    unit = 0
    while unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
        if value < 1024 and float(_three_digits(value)) < 1024:
            break
    return f"{_three_digits(value)} {_UNITS[unit]}"


def human_speed(bytes_per_second: float) -> str:
    """A transfer speed in the same format; ``-- MiB/s`` while nothing is known yet."""
    if bytes_per_second <= 0:
        return "-- MiB/s"
    return f"{human_size(bytes_per_second)}/s"


def check_space(
    path: str | os.PathLike[str],
    needed_bytes: int,
    margin: float = DEFAULT_MARGIN,
) -> None:
    """Raise :class:`BackendError` (code 4) when ``path`` has too little room.

    ``needed_bytes`` is the size still to be written; ``margin`` is extra
    headroom (5% by default) so a nearly-full disk is rejected before a download
    starts. A non-positive need never raises — the caller had nothing to write.
    """
    if needed_bytes <= 0:
        return
    required = int(needed_bytes * (1.0 + max(0.0, margin)))
    available = free_bytes(path)
    if available < required:
        mount = mount_point(path)
        raise BackendError(
            f"Not enough disk space on {mount}: "
            f"need {human_size(required)}, {human_size(available)} free",
            code=4,
        )


__all__ = [
    "DEFAULT_MARGIN",
    "check_space",
    "free_bytes",
    "human_size",
    "human_speed",
    "mount_point",
]
