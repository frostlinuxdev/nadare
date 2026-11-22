# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Start-up sweep of what a killed Nadare leaves behind (#1417).

A crash, a power loss or a logout skips the ``finally`` blocks that normally remove
Nadare's per-run files. Measured on a test desktop: a WebView2 installer copy (215 MB per
crash), per-run download ``.part`` files, the VC++ ``.run-*`` folder, a half-unpacked
runner (~380 MB) and a verified runner tarball. Nothing removed them, ever.

The rule (DECISIONS 2026-11-16): only Nadare's own per-run names, only when older than a
day, and never while their owner lives (the pid in the name or in the owner marker) or while
the download lock is held. Links are never followed.

umu's folder (``~/.local/share/umu``) is shared with the system and other umu apps: only the
temporary folders umu downloads the Steam Runtime into (``.<8 characters>/``, made by
:class:`tempfile.TemporaryDirectory` while umu holds ``umu.lock``) are removed, older than a
day and while the sweep itself holds ``umu.lock``. A killed umu leaves them for good (kill
test 2: 210 MB; one desktop: 985 MB). The runtimes themselves are never touched.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import ownerlease, paths, sandbox

log = logging.getLogger(__name__)

#: Leftovers younger than this may belong to a run still in progress.
MIN_AGE = 24 * 3600

#: ``<name>.<pid>-<hex>.part`` (WebView2), ``.<name>.<pid>.<hex>.part`` (VC++, art, news).
_PID_PART = re.compile(r"\.(\d+)[.-][0-9a-f]{32}\.part$")
#: The private WebView2 installer copy: ``.webview2-run-<pid>-<hex>.exe``.
_WEBVIEW2_RUN = re.compile(r"^\.webview2-run-(\d+)-[0-9a-f]{32}\.exe$")
#: The VC++ installer's private folder (``tempfile.mkdtemp(prefix=".run-")``).
_VC_RUN = re.compile(r"^\.run-[A-Za-z0-9_]+$")
#: A runner download in the shared cache: complete tarball or its ``.part``.
_RUNNER_ASSET = re.compile(r"^[^.].*\.tar\.(?:xz|gz|zst)(?:\.part)?$")
#: The Sandbox's link relay folder; b24 named it without the pid.
_LINK_RELAY = re.compile(r"^nadare-links-(?:(\d+)-)?[A-Za-z0-9_]+$")
_OWNER_MARKER = ".nadare-owner-pid"
#: umu's runtime download folder: ``TemporaryDirectory(prefix=".")`` = "." + 8 tempfile chars.
_UMU_TEMP = re.compile(r"^\.[a-z0-9_]{8}$")
#: Everything umu puts in that folder: the archive, its unpacked tree, the runtime it replaced.
_UMU_TEMP_CONTENT = "SteamLinuxRuntime_"
#: umu holds this (flock) for every runtime download and update.
_UMU_LOCK = "umu.lock"


@dataclass
class Result:
    removed: list[Path] = field(default_factory=list)
    freed: int = 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, another user's
    except (OSError, OverflowError):
        return False
    return True


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _old(st: os.stat_result, now: float, min_age: float) -> bool:
    return now - st.st_mtime >= min_age


def _entries(folder: Path) -> Iterator[Path]:
    """The entries of a real (not linked) folder; nothing when it is missing or a link."""
    st = _lstat(folder)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        yield folder / name


def _size(path: Path, st: os.stat_result) -> int:
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    for root, _dirs, files in os.walk(path):  # os.walk does not follow links by default
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _remove(path: Path, st: os.stat_result, result: Result) -> None:
    size = _size(path, st)
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)  # a link is removed itself, never its target
    except OSError as exc:
        log.debug("could not remove the leftover %s: %s", path, exc)
        return
    result.removed.append(path)
    result.freed += size
    log.info("removed a leftover from an interrupted run: %s (%d bytes)", path, size)


#: flock errors that mean "this filesystem has no locks", not "someone holds it".
_LOCKS_UNSUPPORTED = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP})


def _lock_free(lock: Path) -> bool:
    """True when nobody holds the download's lock (a missing lock file is free)."""
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _LOCKS_UNSUPPORTED:
            return True  # a filesystem without locks: age alone decides (accepted policy)
        # Busy, or the lock could not be inspected at all (EIO, EBADF, ...): an unknown answer
        # is not proof the download is idle, so the file stays (#1468).
        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            log.warning("could not check the download lock %s (%s); keeping its file", lock, exc)
        return False
    finally:
        os.close(fd)  # closing drops our lock too
    return True


def _sweep_cache(cache: Path, now: float, min_age: float, result: Result) -> None:
    for folder in [cache, *_entries(cache)]:
        for entry in _entries(folder):
            st = _lstat(entry)
            if st is None or not _old(st, now, min_age):
                continue
            name = entry.name
            is_dir = stat.S_ISDIR(st.st_mode)
            match = _PID_PART.search(name) or _WEBVIEW2_RUN.match(name)
            if match and not is_dir:
                if not _alive(int(match.group(1))):
                    _remove(entry, st, result)
            elif _VC_RUN.match(name) and (is_dir or stat.S_ISLNK(st.st_mode)):
                _remove(entry, st, result)
            elif folder.name == "runners" and folder != cache and _RUNNER_ASSET.match(name):
                lock_name = "." + name.removesuffix(".part") + ".lock"
                if not is_dir and _lock_free(folder / lock_name):
                    _remove(entry, st, result)


def _sweep_unpacks(runners: Path, now: float, min_age: float, result: Result) -> None:
    for entry in _entries(runners):
        if not entry.name.startswith(".unpack-"):
            continue
        st = _lstat(entry)
        if st is None or not stat.S_ISDIR(st.st_mode) or not _old(st, now, min_age):
            continue
        # The owner's lease, seen from any PID namespace (#1494); unknown waits for age.
        if not ownerlease.may_remove(entry, unknown_min_age=min_age, now=now):
            continue
        _remove(entry, st, result)


def _sweep_link_relays(temp: Path, now: float, min_age: float, result: Result) -> list[Path]:
    before = len(result.removed)
    for entry in _entries(temp):
        match = _LINK_RELAY.match(entry.name)
        if not match:
            continue
        st = _lstat(entry)
        if st is None or not stat.S_ISDIR(st.st_mode) or not _old(st, now, min_age):
            continue
        if match.group(1) and _alive(int(match.group(1))):
            continue
        _remove(entry, st, result)
    return result.removed[before:]


def _umu_folders() -> list[Path]:
    """Owned and shared umu data folders (including old installs)."""
    from . import umu

    roots = umu.runtime_roots()
    if sandbox.in_flatpak():
        # Old Flatpak umu ignored the app's XDG root; only its host shared folder was used.
        return roots[:2]
    return roots


def _sweep_umu_temps(folder: Path, now: float, min_age: float, result: Result) -> None:
    candidates = []
    for entry in _entries(folder):
        if not _UMU_TEMP.match(entry.name):
            continue
        st = _lstat(entry)
        if st is None or not stat.S_ISDIR(st.st_mode) or not _old(st, now, min_age):
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        # Only what umu's download leaves (or nothing): a hidden folder of anything else stays.
        if all(name.startswith(_UMU_TEMP_CONTENT) for name in names):
            candidates.append((entry, st))
    if not candidates:
        return
    # Hold umu's own lock while removing: a umu downloading now keeps its folder, and a umu
    # that starts meanwhile waits instead of racing the removal. Same open flags as umu's.
    try:
        flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(folder / _UMU_LOCK, flags, 0o644)
    except OSError:
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # a umu is downloading or updating right now
        for entry, st in candidates:
            _remove(entry, st, result)
    finally:
        os.close(fd)  # closing drops the lock


def _sweep_runtime_copies(now: float, min_age: float, result: Result) -> None:
    """A killed migration leaves a private copy; never touch a copy still in progress."""
    from . import umu

    base = umu.runtime_base()
    candidates = []
    for entry in _entries(base):
        st = _lstat(entry)
        if (re.fullmatch(r"\.copy-[a-z0-9_]{8}", entry.name) and st is not None
                and stat.S_ISDIR(st.st_mode) and _old(st, now, min_age)):
            candidates.append((entry, st))
    if not candidates:
        return
    fd = os.open(base / ".migration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        for entry, st in candidates:
            _remove(entry, st, result)
    finally:
        os.close(fd)


def _system_temp() -> Path:
    """Where :mod:`tempfile` puts the Sandbox's link relay folders (tests point it elsewhere)."""
    return Path(tempfile.gettempdir())


def sweep(
    *, now: float | None = None, min_age: float = MIN_AGE, temp_dir: Path | None = None
) -> Result:
    """Remove Nadare's crash leftovers; never raises. Returns what was removed."""
    result = Result()
    when = time.time() if now is None else now
    steps = (
        lambda: _sweep_runtime_copies(when, min_age, result),
        lambda: _sweep_cache(paths.cache_dir(), when, min_age, result),
        lambda: _sweep_unpacks(paths.runners_dir(), when, min_age, result),
        lambda: _sweep_link_relays(
            Path(temp_dir) if temp_dir is not None else _system_temp(),
            when,
            min_age,
            result,
        ),
        *(
            (lambda folder=folder: _sweep_umu_temps(folder, when, min_age, result))
            for folder in _umu_folders()
        ),
    )
    for step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 - a start-up tidy-up must never break start-up
            log.debug("leftover sweep step failed", exc_info=True)
    return result


__all__ = ["MIN_AGE", "Result", "sweep"]
