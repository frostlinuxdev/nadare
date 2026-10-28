# SPDX-License-Identifier: GPL-3.0-or-later
"""Point a profile at a Wine prefix the user moved by hand.

Nothing is copied, moved or deleted: the plan only works out which profile fields change,
after checking the new place really looks like the prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RelinkError(ValueError):
    """The new place cannot be used; the message says why."""


@dataclass(frozen=True)
class Relink:
    changes: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def _has_drive_c(path: Path) -> bool:
    return (path / "drive_c").is_dir() or (path / "pfx" / "drive_c").is_dir()


def _under(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def plan_relink(
    profile: dict[str, Any], new_prefix: str | Path, *, made_by_nadare: bool = False
) -> Relink:
    """What changes if ``profile``'s prefix now lives at ``new_prefix``.

    ``made_by_nadare`` is set after Nadare itself moved or copied the folder, so the new
    place is Nadare's even though the old one may still exist.
    """
    new = Path(new_prefix).expanduser()
    if not new.is_dir() or not _has_drive_c(new):
        raise RelinkError(
            f"{new} does not look like a Wine prefix (no drive_c folder). "
            "Pick the folder you moved."
        )
    old_text = str(profile.get("prefix") or "")
    old = Path(old_text).expanduser() if old_text else None
    if old is not None and old.absolute() == new.absolute():
        raise RelinkError("The game already uses that prefix.")

    changes: dict[str, Any] = {"prefix": str(new)}
    notes: list[str] = []
    owned = profile.get("owned_trees")
    recorded = profile.get("owned_paths")
    moved_keys: list[str] = []
    gone = made_by_nadare or (old is not None and not old.exists())
    if isinstance(owned, list) and "prefix" in owned and old is not None and gone:
        moved_keys.append("prefix")

    install = str(profile.get("install_dir") or "")
    if install and old is not None:
        inside = _under(Path(install).expanduser(), old)
        if inside is not None:
            rerooted = new / inside
            if not rerooted.is_dir():
                raise RelinkError(
                    f"The game folder is not at the new place ({rerooted}). "
                    "Move the whole prefix, including the game folder."
                )
            changes["install_dir"] = str(rerooted)
            notes.append(f"Game folder: {rerooted}")
            if isinstance(owned, list) and "install_dir" in owned and gone:
                moved_keys.append("install_dir")

    installed = profile.get("installed_prefix")
    if installed and old is not None and Path(str(installed)).expanduser() == old:
        changes["installed_prefix"] = str(new)  # the backend's uninstall guard compares this

    if moved_keys:
        paths = dict(recorded) if isinstance(recorded, dict) else {}
        for key in moved_keys:
            target = Path(changes[key])
            paths[key] = str(target.resolve())
        changes["owned_paths"] = paths
    return Relink(changes=changes, notes=notes)


def apply(profile: dict[str, Any], plan: Relink) -> None:
    """Write ``plan`` into ``profile`` (the caller saves the config under its lock)."""
    profile.update(plan.changes)
