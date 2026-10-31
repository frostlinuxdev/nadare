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
import threading
from dataclasses import dataclass
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


def tree_bytes(path: str | os.PathLike[str], *, limit: int = 500_000) -> int:
    """Bytes of the regular files under ``path`` (0 when missing); symlinks are not followed.

    Looks at no more than ``limit`` entries: a huge tree only needs to be "large".
    """
    total = 0
    seen = 0
    stack = [str(path)]
    while stack and seen < limit:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    seen += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _device(path: str | os.PathLike[str]) -> int:
    """The id of the filesystem holding ``path`` (0 when it cannot be read)."""
    try:
        return os.stat(_existing(path)).st_dev
    except OSError:
        return 0


@dataclass
class _Promise:
    amount: int  # bytes the install still had to write when it reserved
    free_at_start: int
    path: Path


#: What running installs have promised, per filesystem and owner (#770).
_PROMISES: dict[tuple[int, str], _Promise] = {}
_LOCK = threading.Lock()


def _outstanding(device: int, exclude: str) -> int:
    """Bytes other installs on ``device`` have promised and not written yet.

    A promise shrinks as free space drops: the bytes that left the disk since the install
    reserved are taken as its own. With two installs writing at once that counts the same
    bytes twice, so the estimate errs towards allowing, never towards refusing.
    """
    total = 0
    for (dev, owner), promise in list(_PROMISES.items()):
        if dev != device or owner == exclude:
            continue
        try:
            written = max(0, promise.free_at_start - free_bytes(promise.path))
        except BackendError:
            written = 0
        total += max(0, promise.amount - written)
    return total


class Reservation:
    """Space an install has promised for as long as it runs; use it as a context manager."""

    def __init__(self, owner: str, device: int, promise: _Promise) -> None:
        self.owner = owner
        self._device = device
        self._promise = promise
        self._released = False

    def release(self) -> None:
        with _LOCK:
            if not self._released:
                self._released = True
                key = (self._device, self.owner)
                # Only our own promise: a newer one for the same owner must survive.
                if _PROMISES.get(key) is self._promise:
                    del _PROMISES[key]

    def __enter__(self) -> Reservation:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def reserve(
    path: str | os.PathLike[str],
    needed_bytes: int,
    *,
    owner: str,
    margin: float = DEFAULT_MARGIN,
) -> Reservation:
    """Promise ``needed_bytes`` of ``path``'s filesystem to ``owner`` for one install.

    A lone install is never refused here (the per-file checks keep deciding that, and a size
    estimate must not block an install that would have fitted). It is refused only when other
    running installs have promised so much of the same filesystem that the two cannot both fit.
    """
    needed = max(0, int(needed_bytes))
    device = _device(path)
    with _LOCK:
        others = _outstanding(device, exclude=owner)
        available = free_bytes(path)
        if others > 0 and needed > 0:
            required = int(needed * (1.0 + max(0.0, margin)))
            if available - others < required:
                raise BackendError(
                    f"Not enough disk space on {mount_point(path)} for both installs: this one "
                    f"needs about {human_size(needed)}, {human_size(available)} is free and "
                    f"{human_size(others)} is still promised to other installs. Wait for the "
                    "other install to finish, or choose another folder.",
                    code=4,
                )
        promise = _Promise(needed, available, Path(path))
        _PROMISES[(device, owner)] = promise
    return Reservation(owner, device, promise)


__all__ = [
    "DEFAULT_MARGIN",
    "Reservation",
    "check_space",
    "reserve",
    "tree_bytes",
    "free_bytes",
    "human_size",
    "human_speed",
    "mount_point",
]
