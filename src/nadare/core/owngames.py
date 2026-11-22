# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Retire "your own game" profiles: Nadare runs only the games it installs itself.

Nadare used to add any Wine/Proton game, script or Steam game the user pointed it at. That
feature is gone. A config that still holds such profiles is backed up once, the profiles are
dropped, and the user is told; their game files and prefixes are never touched.
"""

from __future__ import annotations

import copy
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import shortcuts, steamsetup
from .steamstore import normalise_app_id

#: Set once a config had its own-game profiles retired (or had none).
RETIRED_FLAG = "own_games_retired"

#: Backends that only ever ran the user's own files.
_OWN_BACKENDS = frozenset({"wine", "script"})

_BACKUP_SUFFIX = ".before-own-games-removed"


def _app_id(profile: Mapping[str, Any]) -> str:
    extra = profile.get("extra")
    for source in (profile, extra if isinstance(extra, Mapping) else {}):
        for key in ("steam_app_id", "app_id"):
            value = normalise_app_id(str(source.get(key) or "").strip())
            if value is not None:
                return value
    return ""


def is_own_game(profile: Mapping[str, Any]) -> bool:
    """True for a profile Nadare no longer runs: Wine/Script, or a Steam game not in the catalog."""
    backend = str(profile.get("backend") or "")
    if backend in _OWN_BACKENDS:
        return True
    if backend == "steam":
        return steamsetup.game_for(_app_id(profile)) is None if _app_id(profile) else True
    return False


def _backup(path: Path) -> Path:
    target = path.with_name(path.name + _BACKUP_SUFFIX)
    index = 2
    while target.exists():
        target = path.with_name(f"{path.name}{_BACKUP_SUFFIX}-{index}")
        index += 1
    shutil.copy2(path, target)
    return target


def _remove_shortcuts(
    names: list[str],
    applications_dir: str | os.PathLike[str] | None,
    desktop_dir: str | os.PathLike[str] | None,
) -> None:
    """Delete the menu and desktop entries Nadare made for ``names`` (never foreign files)."""
    try:
        desk = desktop_dir if desktop_dir is not None else shortcuts.desktop_dir()
    except OSError:
        desk = None
    for name in names:
        try:
            shortcuts.remove_desktop_entries(
                name, applications_dir=applications_dir, desktop_dir=desk
            )
        except OSError:
            continue  # a dead icon is harmless; the profile is gone either way


def retire(
    cfg: Any,
    *,
    applications_dir: str | os.PathLike[str] | None = None,
    desktop_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Drop own-game profiles once; return a note for the user, or ``""`` if nothing changed.

    The config is backed up first, and the menu/desktop entries Nadare made for those
    profiles are removed. Game files, prefixes and Steam's own shortcut list are left alone.
    """
    if cfg.data.get(RETIRED_FLAG):
        return ""
    names = [name for name, data in cfg.profiles.items() if is_own_game(data)]
    if not names:
        cfg.data[RETIRED_FLAG] = True  # saved with the next change; nothing to write now
        return ""
    path = Path(cfg.path)
    try:
        backup = _backup(path) if path.exists() else None
    except OSError as exc:
        return (
            "Nadare could not back up your settings, so it left your old games as they were: "
            f"{exc}"
        )
    with cfg.locked():
        pending = copy.copy(cfg)
        pending.data = copy.deepcopy(cfg.data)
        pending._snapshot = copy.deepcopy(cfg._snapshot)
        pending._top_snapshot = copy.deepcopy(cfg._top_snapshot)
        pending._removed = cfg._removed.copy()
        for name in names:
            pending.remove_profile(name)
        pending.data[RETIRED_FLAG] = True
        pending.save()
        cfg._publish_settings(cfg.data, pending.data)
        cfg._snapshot = pending._snapshot
        cfg._top_snapshot = pending._top_snapshot
        cfg._removed.clear()
    _remove_shortcuts(names, applications_dir, desktop_dir)
    listed = ", ".join(sorted(names))
    kept = f" A copy of your old settings is in {backup}." if backup else ""
    return (
        "Nadare now only runs the games it installs itself, so it removed your own games from "
        f"the library: {listed}. Their game files and Wine prefixes were not touched; Lutris or "
        f"Bottles can run them.{kept}"
    )
