# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where a game's logs live, and how old ones are pruned (issue #838).

``logs/<game-id>/<operation>-<YYYYMMDD-HHMMSS>.log`` is one run of one operation of one game;
``logs/<game-id>/background.log`` is the small rolling file for status/news/media checks. The
game id comes from the profile key, which is already unique per game copy (a second copy is
``<key>-2``), so every copy has its own folder.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from . import paths

#: Runs kept per operation, and the most one game's folder may hold.
KEEP_RUNS = 5
GAME_CAP_BYTES = 50 * 1024 * 1024
#: The rolling background file keeps about this much of its tail.
BACKGROUND_CAP_BYTES = 256 * 1024

BACKGROUND_FILE = "background.log"

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_RUN_NAME = re.compile(r"(?P<operation>[a-z0-9_]+)-(?P<stamp>\d{8}-\d{6})(?:-(?P<n>\d+))?\.log")


#: What :func:`game_id` makes of a key it has to clean: a slug, a dash and six hex digits.
_GENERATED_ID = re.compile(r".+-[0-9a-f]{6}")


def game_id(profile: str) -> str:
    """A folder name for ``profile`` that is safe on every filesystem.

    A key that is already lowercase and made of ``a-z 0-9 . _ -`` is its own id. Anything else
    (spaces, capitals, Unicode, a leading dot) becomes a lowercase slug plus a short hash of the
    exact key, so two keys that slug alike (or differ only by case, on a case-insensitive disk)
    never share a folder. A safe key that itself looks like such an id (``blue-archive-fc51ab``)
    gets a hash too, or it would share the folder of the key it looks generated from (#972).
    """
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:6]
    if _SAFE_ID.fullmatch(profile) and profile not in {".", ".."}:
        if not _GENERATED_ID.fullmatch(profile):
            return profile
        generated = f"{profile}-{digest}"
        # An older Nadare used the key itself as the folder name. Keep that folder until
        # the hashed one exists (or the old one is gone), so an existing profile's logs do
        # not move out from under it (#972 follow-up).
        logs = paths.logs_dir()
        if not (logs / generated).exists() and (logs / profile).is_dir():
            return profile
        return generated
    slug = re.sub(r"[^a-z0-9._-]+", "-", profile.lower()).strip(".-_")
    slug = slug[:40].strip(".-_") or "game"
    return f"{slug}-{digest}"


def run_log_path(logs: Path, profile: str, operation: str, now: datetime | None = None) -> Path:
    """``<logs>/<game-id>/<operation>-<stamp>.log`` for a new run, never an existing file."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = logs / game_id(profile)
    base = _slug_operation(operation)
    path = folder / f"{base}-{stamp}.log"
    counter = 1
    while path.exists():
        counter += 1
        path = folder / f"{base}-{stamp}-{counter}.log"
    return path


def background_log_path(logs: Path, profile: str) -> Path:
    return logs / game_id(profile) / BACKGROUND_FILE


def _slug_operation(operation: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", operation.lower()).strip("_") or "run"


def operation_of(path: Path) -> str | None:
    """The operation a file in a game folder belongs to, ``"background"`` for the rolling file."""
    if path.name == BACKGROUND_FILE:
        return "background"
    key = _run_key(path)
    return key[0] if key else None


def run_order(path: Path) -> tuple[str, int]:
    """Sort key that puts a game folder's files in time order (same-second runs by counter)."""
    key = _run_key(path)
    return (key[1], key[2]) if key else ("", 0)


def _run_key(path: Path) -> tuple[str, str, int] | None:
    """``(operation, stamp, counter)`` for a file this module wrote, else ``None``."""
    match = _RUN_NAME.fullmatch(path.name)
    if match is None:
        return None
    return match["operation"], match["stamp"], int(match["n"] or 1)


def prune_game(
    folder: Path,
    *,
    keep: int = KEEP_RUNS,
    max_bytes: int = GAME_CAP_BYTES,
    protect: Iterable[Path] = (),
) -> list[Path]:
    """Delete old runs in one game's folder; return what was removed.

    Keeps the newest ``keep`` runs of each operation, then removes the oldest remaining runs
    until the folder fits ``max_bytes``. The newest run overall, any path in ``protect`` (a run
    still being written) and files this module did not write are never removed.
    """
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    protected = {Path(p) for p in protect}
    by_operation: dict[str, list[tuple[tuple[str, int], Path]]] = {}
    for path in entries:
        key = _run_key(path)
        if key is not None and path.is_file():
            by_operation.setdefault(key[0], []).append(((key[1], key[2]), path))

    removed: list[Path] = []
    survivors: list[tuple[tuple[str, int], Path]] = []
    everything = [item for runs in by_operation.values() for item in runs]
    newest = {max(everything, key=lambda item: item[0])[1]} if everything else set()
    for runs in by_operation.values():
        runs.sort(key=lambda item: item[0], reverse=True)  # newest first
        for index, (order, path) in enumerate(runs):
            if index >= keep and path not in protected and _remove(path):
                removed.append(path)
            else:
                survivors.append((order, path))

    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = sum(size(path) for path in entries if path.is_file())
    for _order, path in sorted(survivors, key=lambda item: item[0]):  # oldest first
        if total <= max_bytes:
            break
        if path in newest or path in protected:
            continue
        freed = size(path)
        if _remove(path):
            removed.append(path)
            total -= freed
    return removed


def _remove(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def trim_rolling(path: Path, max_bytes: int = BACKGROUND_CAP_BYTES) -> bool:
    """Cut a rolling file to its last ``max_bytes`` bytes, starting on a whole line."""
    try:
        size = path.stat().st_size
        if size <= max_bytes:
            return False
        with open(path, "rb") as handle:
            handle.seek(size - max_bytes)
            tail = handle.read()
    except OSError:
        return False
    newline = tail.find(b"\n")
    tail = tail[newline + 1 :] if newline != -1 else b""
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(tail)
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True


__all__ = [
    "BACKGROUND_CAP_BYTES",
    "BACKGROUND_FILE",
    "GAME_CAP_BYTES",
    "KEEP_RUNS",
    "background_log_path",
    "game_id",
    "operation_of",
    "prune_game",
    "run_order",
    "run_log_path",
    "trim_rolling",
]
