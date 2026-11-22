# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Games the Flatpak cannot reach, said when the status is read and once at start-up (#1299).

The Flatpak no longer sees the whole host (#1211/#1244). A game that an older Nadare (the
AppImage, or an older Flatpak with ``--filesystem=host``) installed in ``/srv/games`` or ``/data``
does not exist inside it, so its backend reports ``not_installed`` and the page offered INSTALL.
:func:`flatpakfs.unreachable_reason` used to run only when a folder was saved; this runs the same
rule on the folders old profiles already hold. Nothing here writes anything.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import flatpakfs
from .backends import Status
from .config import profile_value


@dataclass(frozen=True)
class OutOfReach:
    profile: str
    title: str
    label: str  # "game folder" or "prefix"
    path: str
    reason: str

    @property
    def key(self) -> str:
        """What a player's "seen it" is remembered under: the game and the folder."""
        return f"{self.profile}\n{self.label}\n{self.path}"


#: The profile keys that hold a game's folders, with the name used for each.
_FOLDERS = (("install_dir", "game folder"), ("prefix", "prefix"), ("wineprefix", "prefix"))


def _folders(profile: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(label, path, reason)`` for each folder of ``profile`` the Flatpak cannot use."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for key, label in _FOLDERS:
        value = profile_value(profile, key)
        if not isinstance(value, str) or not value.strip() or value in seen:
            continue
        seen.add(value)
        path = os.path.expanduser(value)
        try:
            if os.path.exists(path):
                continue  # visible after all: our reading of the grants is not the last word
            reason = flatpakfs.unreachable_reason(path)
        except (OSError, ValueError):
            continue
        if reason:
            found.append((label, path, reason))
    return found


def out_of_reach(profiles: Mapping[str, Any]) -> list[OutOfReach]:
    """Every folder of every game that the Flatpak cannot see (empty outside a Flatpak)."""
    result: list[OutOfReach] = []
    for name, profile in profiles.items():
        if not isinstance(profile, Mapping):
            continue
        title = str(profile.get("title") or name)
        for label, path, reason in _folders(profile):
            result.append(OutOfReach(str(name), title, label, path, reason))
    return result


def explain(profile: Mapping[str, Any] | None, status: Status) -> Status:
    """``status``, or an error that says why when a "not installed" game is only out of reach.

    INSTALL would download the game again into a folder the Flatpak cannot use; RETRY re-reads
    the status after the player allowed the folder and restarted Nadare.
    """
    if status.state != "not_installed" or not isinstance(profile, Mapping):
        return status
    folders = _folders(profile)
    if not folders:
        return status
    _label, path, reason = folders[0]
    data = {**(status.data or {}), "out_of_reach": path}
    return dataclasses.replace(status, state="error", message=reason, data=data)


def status_of(backend: Any) -> Status:
    """``backend.status()`` passed through :func:`explain` with the backend's own profile."""
    profile = getattr(getattr(backend, "ctx", None), "profile", None)
    return explain(profile, backend.status())


def unseen(found: list[OutOfReach], seen: object) -> list[OutOfReach]:
    """The entries the player was not told about yet (``seen`` is the stored list of keys)."""
    known = {str(item) for item in seen} if isinstance(seen, list) else set()
    return [item for item in found if item.key not in known]


def message(found: list[OutOfReach]) -> str:
    lines = "\n".join(f"  • {item.title}: its {item.label} {item.path}" for item in found)
    commands = "\n".join(
        dict.fromkeys(
            f"flatpak override --user --filesystem={item.path} {flatpakfs.APP_ID}" for item in found
        )
    )
    return (
        "The Flatpak version of Nadare cannot see these folders, so these games look not "
        "installed:\n\n"
        f"{lines}\n\n"
        "Your files are still there. To use them, allow each folder once in a terminal and "
        "restart Nadare:\n\n"
        f"{commands}"
    )


__all__ = ["OutOfReach", "explain", "message", "out_of_reach", "status_of", "unseen"]
