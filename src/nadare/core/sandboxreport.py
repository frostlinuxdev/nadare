# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Games whose Sandbox would refuse Play, found at start-up (#1299).

The Sandbox is on by default (b22) and a game folder that would give the hidden home folder back
(home, a folder holding it, a drive top, ``~/.local/share``) stops the launch (2026-11-11). An
older profile never asked about either, so it learned about it only at Play. This lists such
games once, up front, with the same rule the launch uses (:func:`prefixguard.place_reason`).
Nothing here writes anything.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import backends, gamesandbox, prefixguard
from .config import profile_value


@dataclass(frozen=True)
class BroadFolder:
    profile: str
    title: str
    label: str  # "game folder" or "prefix", as the launch error names it
    path: str
    reason: str

    @property
    def key(self) -> str:
        """What a player's "seen it" is remembered under: the game and the folder."""
        return f"{self.profile}\n{self.label}\n{self.path}"


#: The profile keys the launch binds, with the name the launch error uses for each.
_FOLDERS = (("prefix", "prefix"), ("wineprefix", "prefix"), ("install_dir", "game folder"))


def broad_folders(
    profiles: Mapping[str, Mapping[str, Any]], *, home: str | os.PathLike[str] | None = None
) -> list[BroadFolder]:
    """Every folder of a Sandbox-on game that the launch would refuse to bind."""
    found: list[BroadFolder] = []
    for name, profile in profiles.items():
        if not isinstance(profile, Mapping):
            continue
        kind = str(profile.get("backend") or "")
        on = gamesandbox.wanted(
            profile,
            supported=backends.supports_sandbox(kind),
            default=backends.sandbox_default(kind),
            required=backends.sandbox_required(kind),
        )
        if not on:
            continue
        seen: set[str] = set()
        for key, label in _FOLDERS:
            value = profile_value(profile, key)
            if not isinstance(value, str) or not value.strip() or value in seen:
                continue
            seen.add(value)
            path = Path(value).expanduser()
            try:
                reason = prefixguard.place_reason(path, home=home)
            except OSError:
                continue
            if reason:
                title = str(profile.get("title") or name)
                found.append(BroadFolder(str(name), title, label, str(path), reason))
    return found


def unseen(found: list[BroadFolder], seen: object) -> list[BroadFolder]:
    """The entries the player was not told about yet (``seen`` is the stored list of keys)."""
    known = {str(item) for item in seen} if isinstance(seen, list) else set()
    return [item for item in found if item.key not in known]


def message(found: list[BroadFolder]) -> str:
    lines = "\n".join(f"  • {item.title}: its {item.label} is {item.reason}" for item in found)
    return (
        "Sandbox hides your home folder from games, and these games keep files in a place that "
        "would give it back, so Play will stop with an error:\n\n"
        f"{lines}\n\n"
        "Move the game into a library (⋯ → Move to another folder…), or turn Sandbox off for it "
        "on its page."
    )


__all__ = ["BroadFolder", "broad_folders", "message", "unseen"]
