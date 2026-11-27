# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Refuse a game folder on a drive mounted ``noexec`` (#1861, 2026-11-26).

On such a drive Wine cannot map the game's ``.exe`` and ``.dll`` files, so the game dies at
once with an access violation; the player saw only "exit code 5". The fstab ``user`` and
``users`` options switch ``noexec`` on without it being written, which is how drives end up
like this. The check is one ``statvfs`` per folder, so it runs before every install and Play.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .backends import BackendError
from .diskspace import _existing, mount_point


def noexec_mount(path: str | os.PathLike[str]) -> str | None:
    """The mount point when the drive holding ``path`` is mounted noexec, else ``None``.

    A folder that does not exist yet is judged by its nearest existing parent; a drive that
    cannot be read is never blamed (the install or launch reports its own error).
    """
    try:
        candidate = _existing(path)
        if not os.statvfs(candidate).f_flag & os.ST_NOEXEC:
            return None
        return mount_point(candidate)
    except (OSError, ValueError):
        return None


def message(mount: str) -> str:
    return (
        f"Games can't run from {mount}: that drive is mounted with noexec, so Wine can't load "
        "the game's files. In the drive's /etc/fstab line remove noexec, or put exec after "
        "user or users (those turn noexec on), remount the drive, and try again. Or choose a "
        "folder on another drive."
    )


def check(path: str | os.PathLike[str]) -> None:
    """Raise :class:`BackendError` when games cannot run from ``path``."""
    mount = noexec_mount(path)
    if mount is not None:
        raise BackendError(message(mount))


def _folder(backend: Any, name: str) -> Path | None:
    getter = getattr(backend, name, None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:  # noqa: BLE001 - a folder that cannot be told never blocks Play
        return None
    return Path(value) if isinstance(value, (str, os.PathLike)) else None


def guard_install(backend: Any) -> None:
    """Refuse an install or update into a folder games cannot run from."""
    folder = _folder(backend, "install_dir")
    if folder is not None:
        check(folder)


def guard_launch(backend: Any) -> None:
    """Refuse Play when the game's install folder or Wine prefix is on a noexec drive."""
    for name in ("install_dir", "prefix"):
        folder = _folder(backend, name)
        if folder is not None:
            check(folder)
    for folder in _extra_folders(backend):
        check(folder)


def _extra_folders(backend: Any) -> tuple[Path, ...]:
    """A hand-added Wine game's exe folder and prefix (``exec_folders``), if it has them."""
    getter = getattr(backend, "exec_folders", None)
    if not callable(getter):
        return ()
    try:
        folders = getter()
    except Exception:  # noqa: BLE001 - a folder that cannot be told never blocks Play
        return ()
    return tuple(Path(f) for f in folders if isinstance(f, (str, os.PathLike)))


__all__ = ["check", "guard_install", "guard_launch", "message", "noexec_mount"]
