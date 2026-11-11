# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Preview removal of one portable AppImage; never remove user data or directories."""
from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import selfupdate


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


def trash(plan: Plan, move: Callable[[str], tuple[bool, str]]) -> None:
    """Called only after confirmation; failed trashing never becomes permanent deletion."""
    if _identity(plan.path) != plan.identity:
        raise ValueError('The AppImage changed. Open Uninstall again to review its path.')
    success, _ = move(str(plan.path))
    if not success:
        raise OSError('Could not move the AppImage to Trash. No permanent deletion was attempted.')
