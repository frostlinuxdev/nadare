# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Preview removal of one portable AppImage; never remove user data or directories."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import sandbox, selfupdate


@dataclass(frozen=True)
class Plan:
    path: Path
    identity: tuple[int, int, int, int]


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
        raise ValueError('The AppImage path changed or uses a symbolic link.')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def preview() -> Plan | None:
    path = selfupdate.appimage_target()
    if path is None:
        return None
    try:
        return Plan(path, _identity(path))
    except (OSError, ValueError):
        return None


def _source_root() -> Path | None:
    """The checkout this Nadare runs from, when it runs from source; else ``None``."""
    root = Path(__file__).resolve().parents[3]
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def how_to_remove(env: Mapping[str, str] | None = None) -> str:
    """The one step that removes this Nadare when it cannot remove itself."""
    env = os.environ if env is None else env
    if sandbox.in_flatpak():
        app_id = env.get("FLATPAK_ID") or "the Nadare Flatpak"
        return (
            "Nadare is installed as a Flatpak. Remove it in your software centre, or run:\n"
            f"flatpak uninstall {app_id}"
        )
    appimage = env.get("APPIMAGE")
    if appimage:
        return (
            f"This AppImage is {appimage}. Remove it with the tool that installed it "
            "(Gear Lever or AppImageLauncher), or move the file to Trash in your file manager."
        )
    root = _source_root()
    if root is not None:
        return (
            f"This Nadare runs from a source checkout at {root}. Delete that folder to remove "
            "it (its .venv is inside)."
        )
    return "Remove Nadare with the package manager that installed it."


def trash(plan: Plan, move: Callable[[str], tuple[bool, str]]) -> None:
    """Called only after confirmation; failed trashing never becomes permanent deletion."""
    if _identity(plan.path) != plan.identity:
        raise ValueError('The AppImage changed. Open Uninstall again to review its path.')
    success, _ = move(str(plan.path))
    if not success:
        raise OSError('Could not move the AppImage to Trash. No permanent deletion was attempted.')
