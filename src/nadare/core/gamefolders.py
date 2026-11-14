# SPDX-License-Identifier: GPL-3.0-or-later
"""Where a catalog game lives, the Steam way (#1229).

Each catalog game has one game folder: ``<library>/Nadare/<Game>/`` (``profiles/<name>`` in the
default library) holding ``prefix/``, ``install/`` for games Nadare downloads itself, and
``nadare-game.json``. A game the user already had ("I already have it installed") stays where it
is: an import, marked "Outside your libraries" unless it happens to sit in one.

This module only looks. It never moves, deletes or writes a file; :func:`reset_stray` changes
the profile in memory (the caller saves). Existing games keep their folders (maintainer,
2026-11-11): nothing is migrated on disk.

Custom Wine games, scripts and Steam games are outside this model (:func:`placement` returns
``None``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import backends, libraries

#: Profile keys that name a folder of the game.
FOLDER_KEYS = ("install_dir", "prefix")


@dataclass(frozen=True)
class Placement:
    #: The game folder (Nadare's) or, for an import, the folder the game was found in.
    folder: Path | None
    #: The library holding ``folder``; ``None`` = "Outside your libraries".
    library: Path | None
    #: True when Nadare made the game folder (a library game), False for an import.
    made_by_nadare: bool
    #: Profile keys that point outside the game folder, each with the value a reset puts back
    #: (``None``: the key goes, the game does not use it).
    stray: dict[str, str | None] = field(default_factory=dict)


def _real(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(value))))


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def in_model(profile: Mapping[str, Any]) -> bool:
    """True for a catalog game (one game folder); False for custom Wine, scripts and Steam."""
    kind = profile.get("backend")
    return isinstance(kind, str) and libraries._catalog_kind(kind) and backends.one_per_game(kind)


def _launcher_kind(kind: str) -> bool:
    # Launcher games keep their files in the prefix; an install folder is never theirs.
    return kind.startswith("launcher-")


def library_of_folder(config, folder: Path) -> Path | None:
    """The library whose games folder holds ``folder``, or ``None``."""
    default = libraries.default_root()
    for root in libraries.library_roots(config):
        games = root if root == default else root / libraries.NADARE_DIR
        if folder != games and _inside(folder, games):
            return root
    return None


def _folder_values(profile: Mapping[str, Any]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for key in FOLDER_KEYS:
        value = profile.get(key)
        # A hand-edited NUL cannot name a folder; realpath() would raise on it.
        if isinstance(value, str) and value.strip() and "\x00" not in value:
            out[key] = _real(value)
    return out


def _made_folder(profile: Mapping[str, Any], name: str) -> Path | None:
    """The folder Nadare made for this game, when it made one."""
    owned = profile.get("owned_trees")
    dirs = profile.get("owned_dirs")
    if not (isinstance(owned, list) and owned) and not (isinstance(dirs, list) and dirs):
        # Nothing recorded as Nadare's: a library game in the default library still has no
        # folder keys at all (both default inside profiles/<name>); anything else is an import.
        if _folder_values(profile):
            return None
    made = libraries._game_folder(profile, name, None)
    return _real(made) if made is not None else None


def _reset_values(profile: Mapping[str, Any], game: Path) -> dict[str, str]:
    """What each folder key is in the game folder's own manifest."""
    manifest = libraries.read_manifest(game)
    if manifest is None:
        return {}
    out: dict[str, str] = {}
    if manifest.install_dir is not None:
        out["install_dir"] = str(_real(manifest.install_dir))
    if manifest.prefix is not None:
        out["prefix"] = str(_real(manifest.prefix))
    return out


def placement(config, name: str) -> Placement | None:
    """Where the profile ``name`` keeps its game, or ``None`` outside this model."""
    profile = config.profiles.get(name)
    if not isinstance(profile, Mapping) or not in_model(profile):
        return None
    kind = str(profile.get("backend") or "")
    values = _folder_values(profile)
    made = _made_folder(profile, name)
    if made is not None:
        stray: dict[str, str | None] = {}
        resets = _reset_values(profile, made)
        for key, value in values.items():
            if key == "install_dir" and _launcher_kind(kind):
                stray[key] = None
            elif not _inside(value, made):
                stray[key] = resets.get(key, "")
        return Placement(made, library_of_folder(config, made), True, stray)
    folder = values.get("install_dir") or values.get("prefix")
    library = library_of_folder(config, folder) if folder is not None else None
    return Placement(folder, library, False, {})


def reset_stray(config, name: str) -> list[str]:
    """Put stray folder keys back to the game folder's own values; return the keys changed.

    A key without a known value (no manifest names it) is left alone. Nothing on disk changes.
    """
    place = placement(config, name)
    if place is None or not place.stray:
        return []
    profile = config.profiles[name]
    changed: list[str] = []
    for key, value in place.stray.items():
        if value is None:
            profile.pop(key, None)
            changed.append(key)
        elif value:
            profile[key] = value
            changed.append(key)
    return changed


__all__ = [
    "FOLDER_KEYS",
    "Placement",
    "in_model",
    "library_of_folder",
    "placement",
    "reset_stray",
]
