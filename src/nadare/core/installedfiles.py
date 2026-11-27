# SPDX-License-Identifier: GPL-3.0-or-later
"""What the last finished sync of a direct game installed (#1837).

Kuro, BPSR and Yostar remove the files a publisher no longer ships by name. Each finished
sync records here the plan's files with their published size and hash (md5, or Yostar's
CRC-64), so a dropped file is removed only while it is still what Nadare wrote. One a player
changed stays. A name the record does not know is decided as before (an install from an older
Nadare, or a file Kuro's older patch lists name).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import directdl
from .crc64 import crc64_file

log = logging.getLogger(__name__)

NAME = "installed-files.json"
_LIMIT = 64 << 20
_MD5_TEXT = re.compile(r"[0-9a-fA-F]{32}")


def record_path(root: Path) -> Path:
    return Path(root) / directdl.STATE_DIR / NAME


def _entry(entry: Any) -> dict[str, Any] | None:
    """A record entry with a size and a usable hash (md5 preferred), or ``None``."""
    if not isinstance(entry, Mapping):
        return None
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    md5 = entry.get("md5")
    if isinstance(md5, str) and _MD5_TEXT.fullmatch(md5):
        return {"size": size, "md5": md5.lower()}
    crc = entry.get("crc64")
    if isinstance(crc, int) and not isinstance(crc, bool) and 0 <= crc < 1 << 64:
        return {"size": size, "crc64": crc}
    return None


def read(root: Path, game: str) -> dict[str, dict[str, Any]]:
    """``{path: {size, md5 | crc64}}``; empty when absent, a link, too big, damaged or another
    game's. An entry with an unsafe path or no usable size and hash is left out."""
    try:
        fd = os.open(record_path(root), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            raw = handle.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("game") != game:
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return {}
    usable: dict[str, dict[str, Any]] = {}
    for path, entry in files.items():
        checked = _entry(entry)
        if not isinstance(path, str) or checked is None:
            continue
        try:
            safe = directdl.safe_relative_path(path)
        except directdl.DirectError:
            continue
        if safe.split("/", 1)[0] in (directdl.STATE_DIR, directdl.DL_DIR):
            continue
        usable[safe] = checked
    return usable


def write(
    root: Path, game: str, plan: directdl.Plan, kept: Mapping[str, Mapping[str, Any]]
) -> None:
    """Record ``plan``'s files, the dropped files a player changed (``kept``) and any earlier
    entry whose file is still on disk though the plan dropped it, so a later
    removal still knows them. Never raises: a read-only install must not fail a good sync."""
    files: dict[str, dict[str, Any]] = {}
    for rf in plan.files:
        entry = _entry({"size": rf.size, "md5": rf.md5, "crc64": rf.crc64})
        if entry is None:
            continue
        try:
            files[directdl.safe_relative_path(rf.path)] = entry
        except directdl.DirectError:
            continue
    current = {path.lower() for path in files}
    for path, entry in kept.items():
        if path.lower() not in current:
            files[path] = dict(entry)
            current.add(path.lower())
    # A file the plan dropped that is still on disk (no list named it yet) keeps its earlier
    # entry, so a later removal by name still sees whether the player changed it.
    for path, entry in read(root, game).items():
        if path.lower() in current:
            continue
        try:
            (Path(root) / path).lstat()
        except OSError:
            continue  # gone: nothing left to protect
        files[path] = entry
    try:
        directdl._write_record(
            record_path(root), {"game": game, "version": str(plan.version), "files": files}
        )
    except OSError as exc:
        log.debug("%s: could not record the installed files: %s", game, exc)


def _matches(path: Path, size: int, entry: Mapping[str, Any]) -> bool:
    if size != entry["size"]:
        return False
    try:
        if "md5" in entry:
            digest = hashlib.md5()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            return digest.hexdigest() == entry["md5"]
        return crc64_file(path) == entry["crc64"]
    except OSError:
        return False  # unreadable: not proven Nadare's, so not removed by name


def split_changed(
    root: Path, names: Iterable[str], installed: Mapping[str, Mapping[str, Any]]
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """``names`` split into what may be removed and what a player changed (with its entry).

    A name the record does not know may go, as before. A known one may go only while it is a
    plain file with the recorded size and hash, or when nothing is there any more."""
    lowered = {path.lower(): path for path in installed}
    removable: list[str] = []
    kept: dict[str, dict[str, Any]] = {}
    for name in names:
        try:
            safe = directdl.safe_relative_path(name)
        except directdl.DirectError:
            continue  # never a path to remove
        known = safe if safe in installed else lowered.get(safe.lower())
        if known is None:
            removable.append(name)
            continue
        entry = installed[known]
        path = Path(root) / safe
        try:
            info = path.lstat()
        except OSError:
            removable.append(name)  # already gone: nothing to protect
            continue
        if stat.S_ISREG(info.st_mode) and _matches(path, info.st_size, entry):
            removable.append(name)
        else:
            kept[safe] = dict(entry)
    return removable, kept


def kept_note(display_name: str, kept: Mapping[str, Any]) -> str:
    names = sorted(kept)
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return (
        f"{display_name}: kept {len(names)} file(s) the publisher no longer uses, because "
        f"they changed after Nadare installed them: {shown}"
    )
