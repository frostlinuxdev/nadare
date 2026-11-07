# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Read, filter and trim the logs shown by the in-app log viewer.

The GUI never parses log files itself: this module lists the app and
per-operation logs, tails them without cutting a line in half, splits them into
runs at the ``=====`` headers written by :mod:`nadare.gui.workers`, and trims old
runs before a new one starts.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import events, paths, report, runlog

#: The app's own rotating log and its numbered backups.
_APP_LOG = re.compile(r"^nadare\.log(?:\.\d+)?$")
_APP_LABEL = "Nadare (app log)"
_HEADER = re.compile(r"(?m)^=====")
_TAIL_NOTICE = "(older lines not shown)\n"


@dataclass(frozen=True)
class LogFile:
    """One log file on disk, as shown in the viewer's list."""

    path: Path
    label: str
    profile: str | None
    operation: str
    size: int
    mtime: float


def _parse_name(stem: str, profiles: Iterable[str]) -> tuple[str, str]:
    """Split ``<profile>-<operation>`` using the longest known profile name."""
    for name in sorted(profiles, key=len, reverse=True):
        if stem.startswith(f"{name}-"):
            return name, stem[len(name) + 1 :]
    profile, separator, operation = stem.partition("-")
    return profile, operation if separator else ""


def list_logs(
    profiles: Iterable[str],
    directory: Path | str | None = None,
    profile: str | None = None,
) -> list[LogFile]:
    """List app and per-operation logs newest first.

    ``profile`` restricts the result to exactly that profile; a longer profile
    name such as ``CZN-FRESH`` is never treated as ``CZN``.
    """
    folder = Path(directory) if directory is not None else paths.logs_dir()
    known = list(profiles)
    found: list[LogFile] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if _APP_LOG.match(path.name):
            if profile is not None:
                continue
            found.append(
                LogFile(path, _APP_LABEL, None, "app", stat.st_size, stat.st_mtime)
            )
            continue
        if not path.name.endswith(".log"):
            continue
        stem = path.name[:-4]
        parsed_profile, operation = _parse_name(stem, known)
        if profile is not None and parsed_profile != profile:
            continue
        label = (
            f"{parsed_profile} · {operation}" if operation else parsed_profile
        )
        found.append(
            LogFile(
                path,
                label,
                parsed_profile,
                operation,
                stat.st_size,
                stat.st_mtime,
            )
        )
    found.extend(_game_folder_logs(entries, known, profile))
    found.sort(key=lambda item: item.mtime, reverse=True)
    return found


def _game_folder_logs(
    entries: Iterable[Path], known: list[str], profile: str | None
) -> list[LogFile]:
    """The runs in ``<logs>/<game-id>/`` folders, each under the profile its id belongs to."""
    by_id = {runlog.game_id(name): name for name in known}
    found: list[LogFile] = []
    for folder in entries:
        try:
            if not folder.is_dir():
                continue
            files = list(folder.iterdir())
        except OSError:
            continue
        owner = by_id.get(folder.name, folder.name)  # a removed game keeps its folder id
        if profile is not None and owner != profile:
            continue
        for path in files:
            operation = runlog.operation_of(path)
            if operation is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            label = f"{owner} · {operation}"
            found.append(LogFile(path, label, owner, operation, stat.st_size, stat.st_mtime))
    return found


def read_tail(path: Path | str, max_bytes: int = 512 * 1024) -> str:
    """Return the end of ``path``, whole lines only, with a notice when cut."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            prefixed = start > 0
            if prefixed:
                handle.seek(start - 1)
                previous = handle.read(1)
                handle.seek(start)
                data = handle.read()
                if previous != b"\n":
                    newline = data.find(b"\n")
                    data = b"" if newline < 0 else data[newline + 1 :]
            else:
                handle.seek(0)
                data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return (_TAIL_NOTICE + text) if prefixed else text


def sessions(text: str) -> list[str]:
    """Split log text at ``=====`` lines, keeping any leading text as a session."""
    if not text:
        return []
    starts = [match.start() for match in _HEADER.finditer(text)]
    if not starts:
        return [text]
    parts: list[str] = []
    if starts[0] > 0:
        parts.append(text[: starts[0]])
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        parts.append(text[start:end])
    return parts


def filter_lines(text: str, needle: str, case: bool = False) -> str:
    """Keep only lines containing ``needle``; empty ``needle`` returns all."""
    if not needle:
        return text
    wanted = needle if case else needle.lower()
    kept = []
    for line in text.splitlines(keepends=True):
        haystack = line if case else line.lower()
        if wanted in haystack:
            kept.append(line)
    return "".join(kept)


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` through a private temporary file."""
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def trim_sessions(path: Path | str, max_bytes: int = 1_048_576) -> bool:
    """Drop the oldest runs until the file fits, always keeping the newest.

    Returns whether the file changed. A single run larger than ``max_bytes`` is
    left untouched rather than truncated in the middle.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError:
        return False
    if len(raw) <= max_bytes:
        return False
    text = raw.decode("utf-8", errors="replace")
    parts = sessions(text)
    if len(parts) <= 1:
        return False
    keep = list(parts)
    while len(keep) > 1 and len("".join(keep).encode("utf-8")) > max_bytes:
        keep.pop(0)
    if len(keep) == len(parts):
        return False
    try:
        _write_atomic(target, "".join(keep))
    except OSError:
        return False
    return True


def redacted(text: str) -> str:
    """Hide the user's home path, username, hostname and email addresses."""
    return events.mask_known(report.redact(text, mask=False))


#: Each log in a bundle is cut to its last 2 MiB (read_tail keeps whole lines), so a bundle
#: stays small enough to attach to an issue.
BUNDLE_FILE_BYTES = 2 * 1024 * 1024


def bundle(
    profiles: Iterable[str],
    directory: Path | str | None,
    dest: Path | str,
    *,
    profile: str | None = None,
    redact: bool = True,
    max_bytes_per_file: int = BUNDLE_FILE_BYTES,
) -> int:
    """Write every listed log (see :func:`list_logs`) into one zip at ``dest``.

    Returns how many logs went in. Runs keep their game folder in the zip (old flat logs stay
    flat), each log is cut to its end and, with ``redact``, has the username, paths and
    addresses hidden. A ``README.txt`` says what the bundle holds. The zip is written beside
    ``dest`` and moved into place, so a failure leaves no partial file.
    """
    import tempfile
    import zipfile
    from datetime import datetime

    from .. import __version__

    target = Path(dest)
    base = Path(directory) if directory is not None else paths.logs_dir()
    logs = list_logs(profiles, directory, profile=profile)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(temporary)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            count = 0
            used: set[str] = set()
            for log in logs:
                text = read_tail(log.path, max_bytes_per_file)
                try:  # a run keeps its game folder, so two games' "launch-...log" never collide
                    arcname = log.path.relative_to(base).as_posix()
                except ValueError:
                    arcname = log.path.name
                if redact:
                    # A profile name can be an address or a username (#914): hide it in the
                    # entry name too, and keep two names that hide to the same text apart.
                    arcname = redacted(arcname)
                    stem, dot, suffix = arcname.rpartition(".")
                    unique, number = arcname, 1
                    while unique in used or unique == "README.txt":
                        number += 1
                        unique = f"{stem}-{number}{dot}{suffix}" if dot else f"{arcname}-{number}"
                    arcname = unique
                used.add(arcname)
                zf.writestr(arcname, redacted(text) if redact else text)
                count += 1
            hidden = (
                "Your username, home path, host name and public addresses are hidden."
                if redact
                else "Your username and paths are NOT hidden: check before you share it."
            )
            noun = "log file" if count == 1 else "log files"
            shown = (redacted(profile) if redact else profile) if profile else ""
            readme = (
                f"Nadare log bundle\n"
                f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                f"Nadare version: {__version__}\n"
                f"{count} {noun}"
                f"{f' for {shown}' if shown else ''}, each cut to its last "
                f"{max_bytes_per_file // 1024} KiB.\n"
                f"{hidden}\n"
            )
            zf.writestr("README.txt", readme)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return count


__all__ = [
    "BUNDLE_FILE_BYTES",
    "LogFile",
    "bundle",
    "filter_lines",
    "list_logs",
    "read_tail",
    "redacted",
    "sessions",
    "trim_sessions",
]
