# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Move a broken Wine prefix aside without ever deleting user data.

Pure filesystem helpers: no Qt, no network, and no destructive operation. The
old prefix is always renamed (kept) and a failed rebuild is rolled back.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


class PrefixMoveError(RuntimeError):
    """The prefix could not be moved or restored safely."""


def _unique(parent: Path, stem: str) -> Path:
    """``parent/stem``, then ``stem-2``, ``stem-3``... when one already exists."""
    candidate = parent / stem
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = parent / f"{stem}-{index}"
        if not candidate.exists():
            return candidate
        index += 1


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def backup_path(prefix: Path, now: datetime | None = None) -> Path:
    """A unique ``<prefix>.broken-<timestamp>`` path next to ``prefix``."""
    return _unique(prefix.parent, f"{prefix.name}.broken-{_stamp(now)}")


def failed_path(prefix: Path, now: datetime | None = None) -> Path:
    """A unique ``<prefix>.failed-<timestamp>`` path next to ``prefix``."""
    return _unique(prefix.parent, f"{prefix.name}.failed-{_stamp(now)}")


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError as exc:  # pragma: no cover - only on a broken cwd/link
        raise PrefixMoveError(f"could not resolve {path}: {exc}") from exc


def _refusal_reason(prefix: Path) -> str | None:
    """Why ``prefix`` must never be renamed away (``None`` when it is safe)."""
    if prefix == Path("/") or _resolve(prefix) == Path("/"):
        return "the filesystem root"
    home = _resolve(Path.home())
    resolved = _resolve(prefix)
    if resolved == home:
        return "your home folder"
    if resolved in home.parents:
        return "a folder that contains your home folder"
    return None


def move_aside(prefix: Path) -> Path:
    """Rename ``prefix`` to a unique ``.broken-<timestamp>`` sibling.

    Same parent means same filesystem, so this is an atomic rename and never a
    copy or a delete. Returns the backup path.
    """
    if prefix.is_symlink():
        raise PrefixMoveError(
            f"refusing to move {prefix}: it is a symlink. Point the profile at "
            "the real prefix folder first."
        )
    if not prefix.exists():
        raise PrefixMoveError(f"the Wine prefix {prefix} does not exist")
    reason = _refusal_reason(prefix)
    if reason is not None:
        raise PrefixMoveError(f"refusing to move {prefix}: it is {reason}")
    backup = backup_path(prefix)
    try:
        os.rename(prefix, backup)
    except OSError as exc:
        raise PrefixMoveError(
            f"could not move {prefix} aside: {exc}. Nothing was changed."
        ) from exc
    return backup


def restore_inside(backup: Path, prefix: Path, keep: list[Path]) -> list[Path]:
    """Move the kept paths from ``backup`` back under the new ``prefix``.

    Only absolute paths inside the *original* prefix are considered; an
    existing destination is never overwritten. Returns the destinations moved.
    """
    moved: list[Path] = []
    for keep_path in keep:
        if not keep_path.is_absolute():
            continue
        try:
            rel = keep_path.relative_to(prefix)
        except ValueError:
            continue
        if not rel.parts:
            continue
        source = backup / rel
        destination = prefix / rel
        if not source.exists() or destination.exists():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
        except OSError as exc:
            raise PrefixMoveError(
                f"could not restore {destination} from {source}: {exc}"
            ) from exc
        moved.append(destination)
    return moved


def roll_back(backup: Path, prefix: Path, moved: list[Path] | None = None) -> None:
    """Put ``backup`` back at ``prefix``, parking a half-built new prefix first.

    ``moved`` are the destinations :func:`restore_inside` already moved into the
    new prefix; they go back into ``backup`` first so the old prefix is whole
    again and the parked half-built one holds nothing of the user's.
    """
    for destination in moved or []:
        try:
            rel = destination.relative_to(prefix)
        except ValueError:
            continue
        target = backup / rel
        if not destination.exists() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(destination, target)
        except OSError as exc:
            raise PrefixMoveError(
                f"could not move {destination} back into the old prefix: {exc}. "
                f"The old prefix is still at {backup}."
            ) from exc
    if prefix.exists() or prefix.is_symlink():
        parked = failed_path(prefix)
        try:
            os.rename(prefix, parked)
        except OSError as exc:
            raise PrefixMoveError(
                f"could not move the half-built prefix {prefix} aside: {exc}. "
                f"The old prefix is still at {backup}."
            ) from exc
    try:
        os.rename(backup, prefix)
    except OSError as exc:
        raise PrefixMoveError(
            f"could not put the old prefix back at {prefix}: {exc}. "
            f"It is still at {backup}."
        ) from exc
