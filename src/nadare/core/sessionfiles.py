# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in session files an interrupted run may have left on disk (#1231).

A launch deletes its session file when it ends (``finally``, ``atexit``, the next launch), but a
hard kill or power loss can leave one behind, in Nadare's profile folders (CZN) or in a game's
Wine prefix (NIKKE). Nothing here reads or copies them: callers only learn that they exist, so
the support report can tell the user to delete them before sharing a folder.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The file names a run writes its live (or expired) session to.
SESSION_NAMES = frozenset({"session.json", "session.enc.json", "session.json.expired-bak"})
#: Where they live below a profile's data folder (CZN's stub and auth worker).
_PROFILE_SUBDIRS = ("stub", "auth")
#: NIKKE's per-run session folders, below its prefix (``nikke.hosts.STAGE_PARTS``).
_NIKKE_STAGE = ("drive_c", "Games", ".nadare", "nikke")
_MAX_RUN_DIRS = 200


def _is_plain_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_plain_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _session_files_in(folder: Path) -> list[Path]:
    if not _is_plain_dir(folder):
        return []
    return [folder / name for name in sorted(SESSION_NAMES) if _is_plain_file(folder / name)]


def find_leftovers(profiles: Mapping[str, Any], profiles_root: Path) -> list[Path]:
    """Session files on disk for ``profiles`` (never following a link)."""
    found: list[Path] = []
    root = Path(profiles_root)
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
            continue
        for sub in _PROFILE_SUBDIRS:
            found += _session_files_in(root / name / sub)
        prefix = profile.get("prefix") if isinstance(profile, Mapping) else None
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        stage = Path(prefix).joinpath(*_NIKKE_STAGE)
        # Every part down to the stage must be a real folder: a link is not followed.
        if not all(_is_plain_dir(Path(prefix).joinpath(*_NIKKE_STAGE[:i])) for i in range(5)):
            continue
        try:
            runs = sorted(stage.iterdir())[:_MAX_RUN_DIRS]
        except OSError:
            continue
        for run in runs:
            if run.name.startswith("session-"):
                found += _session_files_in(run)
    return found


__all__ = ["SESSION_NAMES", "find_leftovers"]
