# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where a Wine prefix or a game folder may not be.

One place for the rule that Add Game, Settings, the Sandbox and the prefix rebuild all need:
the top of a drive, the home folder (or a folder holding it), a personal folder such as
Documents, or a folder another game already uses is never a game's own folder. A drive is a fine
*library* to put ``Nadare/<Game>`` in; it is not the prefix itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Folders under the home that hold the user's own files.
PERSONAL_FOLDERS = frozenset(
    {"Documents", "Downloads", "Desktop", "Pictures", "Music", "Videos", "Public", "Templates"}
)
#: Profile keys that name a folder the game owns.
_FOLDER_KEYS = ("prefix", "wineprefix", "install_dir")


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def is_wine_prefix(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` already looks like a Wine or Proton prefix."""
    folder = Path(path)
    return (
        (folder / "drive_c").is_dir()
        or (folder / "pfx").is_dir()
        or (folder / "system.reg").is_file()
    )


def _is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except OSError:
        return False


def broad_reason(path: str | os.PathLike[str], home: str | os.PathLike[str] | None = None) -> str:
    """Why binding ``path`` into the Sandbox would undo hiding ``home``, or ``""``."""
    resolved = _real(path)
    real_home = _real(home if home is not None else Path.home())
    if resolved == Path("/"):
        return "the top of the filesystem"
    if resolved == real_home:
        return "your home folder"
    if resolved in real_home.parents:
        return "a folder that contains your home folder"
    if os.path.ismount(resolved):
        return "the top of a drive or partition"
    return ""


def too_broad_to_bind(
    path: str | os.PathLike[str], home: str | os.PathLike[str] | None = None
) -> bool:
    """True when binding ``path`` into the Sandbox would undo hiding the home folder."""
    return bool(broad_reason(path, home))


def unsafe_reason(
    path: str | os.PathLike[str] | None,
    *,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
    own: str = "",
    as_prefix: bool = False,
) -> str:
    """Why ``path`` must not be a game's folder, or ``""`` when it may be.

    ``profiles`` and ``own`` find folders other games already use; ``as_prefix`` also refuses
    an existing folder that holds other files and is not a Wine prefix.
    """
    if not path or not str(path).strip():
        return ""
    resolved = _real(path)
    home = _real(Path.home())
    if resolved == Path("/"):
        return "the top of the filesystem"
    if os.path.ismount(resolved):
        return "the top of a drive or partition (put the game in a folder inside it)"
    if resolved == home:
        return "your home folder"
    if resolved in home.parents:
        return "a folder that contains your home folder"
    if resolved.parent == home and resolved.name in PERSONAL_FOLDERS:
        return "one of your personal folders"
    for name, profile in (profiles or {}).items():
        if name == own or not isinstance(profile, Mapping):
            continue
        for key in _FOLDER_KEYS:
            value = profile.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            other = _real(value)
            # A legacy profile saved with a folder that can be nobody's game folder (home, a
            # folder holding it, / or a drive) must not claim everything below it (#1207 b077).
            broad = other == home or other in home.parents or os.path.ismount(other)
            if broad and resolved != other:
                continue
            if resolved == other or resolved in other.parents or other in resolved.parents:
                title = str(profile.get("title") or name)
                return f"{_USED_BY}{title}"
    if as_prefix and resolved.is_dir() and not is_wine_prefix(resolved) and not _is_empty(resolved):
        return _HOLDS_OTHER_FILES
    return ""


_HOLDS_OTHER_FILES = "a folder that holds other files and is not a Wine prefix"
_USED_BY = "already used by "


def explain(path: str | os.PathLike[str], reason: str) -> str:
    """A plain sentence for a refused ``path``, with advice that fits the reason."""
    if reason.startswith(_USED_BY):
        # A folder inside another game's folder is refused as well, so do not suggest one.
        return f"{path} is {reason}. Each game needs its own folder: pick a different one."
    if reason == _HOLDS_OTHER_FILES:
        return (
            f"{path} is {reason}. Pick an empty folder (or a new one), or the game's "
            "existing Wine prefix."
        )
    return f"{path} is {reason}. Pick a folder inside it instead, for example Nadare/<game>."
