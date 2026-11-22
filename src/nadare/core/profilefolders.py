# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Folders under ``profiles/`` that no game uses any more (#1417 phase 2e).

Removing a game with "delete its data" unticked keeps ``profiles/<name>``: its prefix,
settings and maybe saves. Once that game is gone from Nadare nothing ever looks at the
folder again. Settings lists these folders with their size so the player can delete them;
nothing here deletes on its own, because they may hold saves. A folder that holds nothing
at all (only empty folders) is not offered.

A folder is offered only when it is a real folder directly in ``profiles/`` (not a link,
not hidden), is not the default library's ``Nadare`` folder, has no profile of its name, no
game records a folder that overlaps it (:func:`location.overlapping`), and it neither holds
nor sits in a library the player added. :func:`remove` runs the same checks again right
before deleting.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diskspace, location, paths
from .paths import real as _real

log = logging.getLogger("nadare.profilefolders")


@dataclass(frozen=True)
class Folder:
    """A folder no game uses: where it is and the bytes it holds."""

    path: Path
    size: int


def _overlaps_a_library(folder: Path, roots: Iterable[str | os.PathLike[str]], home: Path) -> bool:
    """``folder`` is, holds or sits in a library the player added.

    The default library is ``profiles/`` itself, so it holds every candidate: only the
    other roots count.
    """
    for root in roots:
        try:
            real = _real(root)
        except (OSError, ValueError):
            return True  # an unreadable library entry: refuse rather than guess
        if real == home:
            continue
        if real == folder or folder in real.parents or real in folder.parents:
            return True
    return False


def _is_orphan(
    entry: Path,
    home: Path,
    profiles: Mapping[str, Any],
    roots: Iterable[str | os.PathLike[str]],
) -> bool:
    name = entry.name
    if not name or name.startswith(".") or name == location.NADARE_DIR or name in profiles:
        return False
    try:
        if entry.is_symlink() or not entry.is_dir():
            return False
        real = home / name
        if _overlaps_a_library(real, roots, home):
            return False
        claim = location.overlapping(
            profiles, real, excluding=None, direction="either", ignore_broad=False
        )
    except (OSError, ValueError, RuntimeError):
        return False
    return claim is None


def _holds_anything(folder: Path) -> bool:
    """True unless ``folder`` holds nothing but empty folders (no file, link or device).

    A folder of nothing is not worth a "may hold saves" question (#1417 2e). It stays on
    disk: this module never deletes on its own. An unreadable folder counts as holding
    something, so it is still shown.
    """
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        return True
        except OSError:
            return True
    return False


def _home() -> Path | None:
    root = paths.profiles_dir()
    try:
        if not root.is_dir():
            return None
        return root.resolve()
    except OSError:
        return None


def find(
    profiles: Mapping[str, Any], library_roots: Iterable[str | os.PathLike[str]]
) -> list[Folder]:
    """The folders in ``profiles/`` no game uses, sorted by name, each with its size."""
    home = _home()
    if home is None:
        return []
    roots = list(library_roots)
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return []
    return [
        Folder(entry, diskspace.tree_bytes(entry))
        for entry in entries
        if _is_orphan(entry, home, profiles, roots) and _holds_anything(entry)
    ]


def remove(
    folder: str | os.PathLike[str],
    profiles: Mapping[str, Any],
    library_roots: Iterable[str | os.PathLike[str]],
) -> bool:
    """Delete ``folder`` when it is still one :func:`find` would offer; True when it went."""
    home = _home()
    if home is None:
        return False
    given = Path(folder)
    # Only a direct child of profiles/, named plainly: no "..", no link, no nested folder.
    if given.name in ("", ".", "..") or ".." in given.parts:
        return False
    try:
        if given.parent.resolve() != home:
            return False
    except OSError:
        return False
    entry = home / given.name
    if not _is_orphan(entry, home, profiles, list(library_roots)):
        return False
    from . import removal  # mount-aware, like Remove (#1481)

    try:
        outcome = removal.delete_tree(entry)
    except OSError:
        log.warning("could not delete the unused game folder %s", entry, exc_info=True)
        return False
    for mount in outcome.mounts:
        log.warning("kept %s: a mounted drive inside an unused game folder", mount)
    for path, exc in outcome.leftovers:
        log.warning("could not delete %s: %s", path, exc)
    if outcome.mounts or outcome.leftovers or entry.exists():
        return False
    log.info("deleted the folder of a removed game: %s", entry)
    return True


__all__ = ["Folder", "find", "remove"]
