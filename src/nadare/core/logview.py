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

from . import events, paths, report

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
    found.sort(key=lambda item: item.mtime, reverse=True)
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


__all__ = [
    "LogFile",
    "filter_lines",
    "list_logs",
    "read_tail",
    "redacted",
    "sessions",
    "trim_sessions",
]
