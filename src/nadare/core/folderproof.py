# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove, after a restart, that a folder is the very one Nadare recorded.

A folder's device and inode number are no proof once Nadare stops watching it: ext4 and other
file systems give a deleted folder's inode number to the next folder made, so a different
folder the player puts at the same path can carry the recorded pair. b25 trusted the pair
alone, and GitHub's ext4 runners showed it (tmpfs, where the tests ran here, never reuses one).

A proof is the device and inode **plus** a random token that Nadare writes into a small marker
file inside the folder (:func:`stamp`). A folder someone else made has no such file; a copy of
the folder (a backup) has another inode. :func:`matches` needs both. Every record that lets a
later start delete, put back or adopt a folder goes through this module, and
``tests/test_folderproof_guard.py`` fails on any other use of ``st_ino`` that is not listed
there with a reason.

A missing, unreadable or legacy (token-less) proof never matches: the caller then keeps the
folder and its record, which is always the safe side.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Each kind of record has its own marker, so one folder can carry several at once (a Move's
#: original is also a moved folder) without one overwriting another's token.
ROLES = ("moved", "original", "move", "import", "rebuild")
_PREFIX = ".nadare-proof-"
#: Every marker name: the Move's file-by-file comparisons leave these out.
MARKER_NAMES = frozenset(_PREFIX + role for role in ROLES)
_TOKEN_BYTES = 16
_MAX_READ = 256


@dataclass(frozen=True)
class Proof:
    dev: int
    ino: int
    token: str

    def record(self) -> dict[str, Any]:
        """The JSON form kept in the config or a journal."""
        return {"id": [self.dev, self.ino], "token": self.token}


def marker_name(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown folder proof role {role!r}")
    return _PREFIX + role


def new_token() -> str:
    return secrets.token_hex(_TOKEN_BYTES)


def valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 2 * _TOKEN_BYTES
        and all(c in "0123456789abcdef" for c in value)
    )


def _id_pair(value: object) -> tuple[int, int] | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        return value[0], value[1]
    return None


def identity(path: str | os.PathLike) -> tuple[int, int] | None:
    """``(device, inode)`` of the folder itself (a link is not followed), else ``None``.

    Only for checks inside one running operation; anything kept across a restart is a
    :class:`Proof`.
    """
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def _open_dir(path: str | os.PathLike) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def stamp(path: str | os.PathLike, role: str, token: str | None = None) -> Proof:
    """Write ``role``'s marker with a new (or the given) token into the folder ``path``.

    The marker replaces any earlier one atomically and is synced before this returns, so a
    record saved afterwards never names a token that a crash could lose. Raises ``OSError``
    (the folder is a link, not a folder, read-only, ...); ``ValueError`` for a bad role/token.
    """
    name = marker_name(role)
    token = new_token() if token is None else token
    if not valid_token(token):
        raise ValueError("bad folder proof token")
    dir_fd = _open_dir(path)
    try:
        info = os.fstat(dir_fd)
        temp = f"{name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=dir_fd,
        )
        try:
            try:
                os.write(fd, (token + "\n").encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.fsync(dir_fd)
        return Proof(info.st_dev, info.st_ino, token)
    finally:
        os.close(dir_fd)


def try_stamp(path: str | os.PathLike, role: str, token: str | None = None) -> Proof | None:
    """:func:`stamp`, or ``None`` when the folder cannot take a marker (the record then
    carries no proof and nothing is ever done to the folder on its strength)."""
    try:
        return stamp(path, role, token)
    except (OSError, ValueError):
        return None


def read_token(path: str | os.PathLike, role: str) -> str | None:
    """The token in ``role``'s marker inside ``path``, or ``None``."""
    try:
        dir_fd = _open_dir(path)
    except (OSError, ValueError):
        return None
    try:
        return _read_token(dir_fd, marker_name(role))
    finally:
        os.close(dir_fd)


def _read_token(dir_fd: int, name: str) -> str | None:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, _MAX_READ).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return text if valid_token(text) else None


def matches(path: str | os.PathLike, role: str, record: Mapping[str, Any] | None) -> bool:
    """Whether ``path`` is still the folder ``record`` (from :meth:`Proof.record`) proves.

    Same device and inode as recorded, not a link, and ``role``'s marker inside holds the
    recorded token. Anything missing, odd or unreadable is ``False``.
    """
    if not isinstance(record, Mapping):
        return False
    pair, token = _id_pair(record.get("id")), record.get("token")
    if pair is None or not valid_token(token):
        return False
    name = marker_name(role)
    try:
        dir_fd = _open_dir(path)
    except (OSError, ValueError):
        return False
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != pair:
            return False
        found = _read_token(dir_fd, name)
    except OSError:
        return False
    finally:
        os.close(dir_fd)
    return found is not None and hmac.compare_digest(found, token)


def record_of(pair: object, token: object) -> dict[str, Any] | None:
    """A record from a stored ``[dev, ino]`` and token (older record layouts), or ``None``."""
    ids = _id_pair(pair)
    if ids is None or not valid_token(token):
        return None
    return {"id": list(ids), "token": token}


def clear(path: str | os.PathLike, role: str) -> None:
    """Remove ``role``'s marker from ``path`` if it is there (best effort, never raises)."""
    try:
        dir_fd = _open_dir(path)
    except (OSError, ValueError):
        return
    try:
        os.unlink(marker_name(role), dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)




__all__ = [
    "MARKER_NAMES",
    "ROLES",
    "Proof",
    "clear",
    "identity",
    "marker_name",
    "matches",
    "new_token",
    "read_token",
    "record_of",
    "stamp",
    "try_stamp",
    "valid_token",
]
