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

import fcntl
import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .backends import BackendError

log = logging.getLogger(__name__)

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


def install_space(
    backend: object, margin: float = DEFAULT_MARGIN
) -> tuple[int, Path] | None:
    """Refuse a fresh install that cannot fit where ``backend`` writes it.

    The size is the chosen install variant (e.g. WuWa's quality) when the backend offers
    one; what is already on disk (a resumed install) counts as written. Returns
    ``(size, folder)`` for the caller's wording, or ``None`` when either is unknown.
    """
    from . import backends as core_backends

    kind = str(getattr(backend, "kind", "") or "")
    folder = getattr(backend, "install_dir", None)
    size = getattr(backend, "install_size_hint", None)
    if not size or not callable(folder):
        return None
    asked = core_backends.install_choice(kind) if kind else None
    if asked is not None:
        option, sizes = asked
        profile = getattr(getattr(backend, "ctx", None), "profile", None)
        if isinstance(profile, dict):
            # Top level, else ``extra`` (the backends' rule; core must not import backends).
            chosen = profile.get(option.key)
            extra = profile.get("extra")
            if chosen in (None, "") and isinstance(extra, dict):
                chosen = extra.get(option.key)
            if chosen in (None, ""):
                chosen = option.default
            # Trimmed and case-blind, as the backends read it ("UHD" is uhd; stress s3 F4).
            choice_size = sizes.get(str(chosen).strip().lower()) or sizes.get(
                str(option.default).strip().lower()
            )
            if choice_size:
                size = choice_size
    path = Path(folder())
    needed = max(0, int(size) - tree_bytes(path))
    check_space(path, needed, margin=margin)
    return int(size), path


def _device(path: str | os.PathLike[str]) -> int:
    """The id of the filesystem holding ``path`` (0 when it cannot be read)."""
    try:
        return os.stat(_existing(path)).st_dev
    except OSError:
        return 0


def _reservation_file(path: str | os.PathLike[str]) -> Path:
    """The shared record for reservations on the filesystem holding ``path``.

    Keyed by filesystem, not folder: two installs into one folder are already refused by
    the install lock; the case to catch is two processes filling one disk with two games.
    """
    digest = hashlib.sha256(str(_device(path)).encode("utf-8")).hexdigest()[:32]
    return paths.state_dir() / "reservations" / f"{digest}.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_records(handle) -> list[dict[str, object]]:
    """Valid records still owned by a live process (dead pids are ignored)."""
    handle.seek(0)
    try:
        payload = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    records: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            record = {
                "pid": int(item["pid"]),
                "owner": str(item["owner"]),
                "device": int(item["device"]),
                "amount": int(item["amount"]),
                "free_at_start": int(item["free_at_start"]),
                "path": str(item["path"]),
                "token": str(item["token"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if _pid_alive(int(record["pid"])):
            records.append(record)
    return records


def _write_records(handle, records: list[dict[str, object]]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(records, handle)
    handle.flush()
    os.fsync(handle.fileno())


def _open_records(path: Path):
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        log.debug("could not open disk reservation record %s: %s", path, exc)
        if handle is not None:
            handle.close()
        return None
    return handle


def _close_records(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def _file_outstanding(records: list[dict[str, object]], device: int, exclude: str) -> int:
    """Bytes promised by other processes on this install folder."""
    total = 0
    me = os.getpid()
    for record in records:
        if int(record["pid"]) == me:  # this process's _PROMISES already counts it
            continue
        if int(record["device"]) != device or str(record["owner"]) == exclude:
            continue
        try:
            written = max(0, int(record["free_at_start"]) - free_bytes(str(record["path"])))
        except BackendError:
            written = 0
        total += max(0, int(record["amount"]) - written)
    return total


@dataclass
class _Promise:
    amount: int  # bytes the install still had to write when it reserved
    free_at_start: int
    path: Path
    token: str


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

    def __init__(
        self, owner: str, device: int, promise: _Promise, record: Path
    ) -> None:
        self.owner = owner
        self._device = device
        self._promise = promise
        self._record = record
        self._released = False

    def _release_record(self) -> None:
        handle = _open_records(self._record)
        if handle is None:
            return
        try:
            records = _read_records(handle)
            kept = [r for r in records if str(r["token"]) != self._promise.token]
            if len(kept) != len(records):
                try:
                    _write_records(handle, kept)
                except OSError as exc:
                    log.debug("could not update disk reservation record: %s", exc)
        finally:
            _close_records(handle)

    def release(self) -> None:
        with _LOCK:
            if self._released:
                return
            self._released = True
            key = (self._device, self.owner)
            # Only our own promise: a newer one for the same owner must survive.
            if _PROMISES.get(key) is self._promise:
                del _PROMISES[key]
            self._release_record()

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
    record_file = _reservation_file(path)
    with _LOCK:
        handle = _open_records(record_file)
        try:
            records = _read_records(handle) if handle is not None else []
            others = _outstanding(device, exclude=owner) + _file_outstanding(
                records, device, owner
            )
            available = free_bytes(path)
            if others > 0 and needed > 0:
                required = int(needed * (1.0 + max(0.0, margin)))
                if available - others < required:
                    raise BackendError(
                        f"Not enough disk space on {mount_point(path)} for both installs: this "
                        f"one needs about {human_size(needed)}, {human_size(available)} is free "
                        f"and {human_size(others)} is still promised to other installs. Wait for "
                        "the other install to finish, or choose another folder.",
                        code=4,
                    )
            promise = _Promise(needed, available, Path(path), uuid.uuid4().hex)
            _PROMISES[(device, owner)] = promise
            if handle is not None:
                records = [r for r in records if str(r["owner"]) != owner]
                records.append(
                    {
                        "pid": os.getpid(),
                        "owner": owner,
                        "device": device,
                        "amount": needed,
                        "free_at_start": available,
                        "path": str(promise.path),
                        "token": promise.token,
                    }
                )
                try:
                    _write_records(handle, records)
                except OSError as exc:
                    log.debug("could not write disk reservation record: %s", exc)
        finally:
            _close_records(handle)
    return Reservation(owner, device, promise, record_file)


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
