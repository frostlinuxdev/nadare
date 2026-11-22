# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Replace a small file atomically: a private temp file beside it, fsync, then rename (#1455).

One writer for Nadare's small files (registry files, configs, caches, journals). The copies it
replaces disagreed on the things that matter: whether the data reaches the disk before the new
name points at it (the Wine registry writers did not, so a crash could leave an empty
``user.reg``), whether a failed write leaves its temp file behind, and whether a symlinked
config stays a symlink.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write(
    path: str | os.PathLike[str],
    data: str | bytes,
    *,
    mode: int | None = None,
    follow_symlink: bool = False,
    sync_dir: bool = False,
    make_parent: bool = False,
) -> None:
    """Replace ``path`` with ``data``; on any failure the old file is untouched.

    Text is written as UTF-8 with ``surrogateescape`` (a registry file read the same way
    round-trips byte for byte) and no newline translation. ``mode`` is the new file's
    permissions; ``None`` keeps an existing file's, and a new file is private (0600).
    ``follow_symlink`` replaces the file a symlink points at (a dotfiles-managed config stays a
    link); otherwise the link itself is replaced. ``sync_dir`` also syncs the folder, so the
    rename itself survives a crash.
    """
    target = Path(path)
    if follow_symlink:
        target = Path(os.path.realpath(target))
    if make_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else bytes(data)
    if mode is None:
        try:
            mode = target.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    if sync_dir:
        sync_folder(target.parent)


def sync_folder(folder: str | os.PathLike[str]) -> None:
    """Best-effort fsync of a folder, so a rename in it survives a crash."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
