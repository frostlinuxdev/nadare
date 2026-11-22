# SPDX-License-Identifier: GPL-3.0-or-later
"""Ownership of a hidden work folder that every PID namespace can see (#1494).

Runner staging (``.unpack-*``) and swap backups (``.<runner>.old-*``) sit in a folder that
another Nadare (a Flatpak next to an AppImage, a sandboxed copy) may tidy up. A PID in the
marker means nothing in another PID namespace, so the owner also holds a ``flock`` on the
marker for as long as it works on the folder: every process on the same filesystem sees that
lock, whatever its namespace, and the kernel drops it when the owner dies.

:func:`may_remove` keeps a folder whose lease is held, whose PID is alive here, or whose
ownership it cannot prove (no marker, an old bare-PID marker, no ``flock`` on the filesystem,
an unreadable marker) until it is older than the caller's limit.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: The marker file inside the owned folder: ``"<pid> lease"`` when a lease is held.
MARKER = ".nadare-owner-pid"
_TAG = "lease"
_NO_LOCKS = {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}


class LeaseHeld(OSError):
    """Another live process already holds the lease on this folder."""


class Lease:
    """A held lease; :meth:`release` (or the owner's death) lets it go."""

    def __init__(self, fd: int | None) -> None:
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)  # closing the only descriptor drops the flock
            finally:
                self._fd = None


def take(directory: Path) -> Lease:
    """Write the marker in ``directory`` and hold its lease; raises :class:`LeaseHeld`."""
    fd = os.open(
        directory / MARKER,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    locked = True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LeaseHeld(errno.EBUSY, f"{directory} is in use by another process") from exc
        except OSError as exc:
            if exc.errno not in _NO_LOCKS:
                raise
            locked = False  # no flock here: the PID and the folder's age decide, as before
        text = f"{os.getpid()} {_TAG}\n" if locked else f"{os.getpid()}\n"
        os.ftruncate(fd, 0)
        os.pwrite(fd, text.encode(), 0)
    except BaseException:
        os.close(fd)
        raise
    return Lease(fd)


def _alive_here(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _age(directory: Path, marker_mtime: float | None, now: float) -> float:
    newest = marker_mtime or 0.0
    try:
        newest = max(newest, os.lstat(directory).st_mtime)
    except OSError:
        return 0.0
    return now - newest


def may_remove(directory: Path, *, unknown_min_age: float, now: float | None = None) -> bool:
    """Whether no live owner can be using ``directory`` (see the module's text)."""
    now = time.time() if now is None else now
    marker = directory / MARKER
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return _age(directory, None, now) >= unknown_min_age
    except OSError:
        return False  # a link, a folder, no permission: not provably free
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return False
        try:
            text = os.read(fd, 64).decode("ascii", "replace").split()
        except OSError:
            return False
        pid = int(text[0]) if text and text[0].isdigit() else 0
        if pid and _alive_here(pid):
            return False
        if len(text) > 1 and text[1] == _TAG:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False  # the owner is alive, in whatever namespace
            except OSError as exc:
                if exc.errno not in _NO_LOCKS:
                    return False
            else:
                return True  # the lease was let go: its owner is gone
        # A bare PID (older versions) or no locks here: only age can tell.
        return _age(directory, info.st_mtime, now) >= unknown_min_age
    finally:
        os.close(fd)


__all__ = ["MARKER", "Lease", "LeaseHeld", "may_remove", "take"]
