# SPDX-License-Identifier: GPL-3.0-or-later
"""Move a folder (a Wine prefix) to another place without risking the original.

On one disk it is a rename. Across disks the folder is copied next to its destination,
checked against the source, and only then renamed into place; the source is left alone
(the caller tells the user it can be deleted). Symlinks are copied as links, never followed.
Nothing is deleted except the partial copy this call made itself.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import diskspace, prefixmove
from .events import Cancel

#: Left free on the destination disk besides the size of the copy.
SPACE_MARGIN = 1 << 30
PARTIAL_SUFFIX = ".nadare-partial"
CHUNK = 1 << 20


class RelocateError(RuntimeError):
    """The move was refused or failed; nothing of the user's was changed."""


@dataclass(frozen=True)
class Moved:
    renamed: bool
    kept_old: Path | None


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def _same_device(src: Path, dst: Path) -> bool:
    return os.stat(src).st_dev == os.stat(_nearest_existing(dst.parent)).st_dev


def _walk_error(exc: OSError) -> None:
    """``os.walk`` hides a folder it cannot list; a move must not, or the copy would "verify"."""
    raise exc


def _summary(root: Path) -> tuple[int, int, int, int, int]:
    """``(files, dirs, links, bytes, newest mtime)`` below ``root``.

    Only regular files, folders and symlinks count: a FIFO, socket or device is never copied.
    """
    files = dirs = links = size = newest = 0
    for current, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        for name in list(names):
            path = Path(current, name)
            if path.is_symlink():
                links += 1
                names.remove(name)  # a link to a directory is still just a link
            else:
                dirs += 1
        for name in filenames:
            info = os.lstat(Path(current, name))
            if stat.S_ISLNK(info.st_mode):
                links += 1
            elif stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
                newest = max(newest, info.st_mtime_ns)
    return files, dirs, links, size, newest


def _check(src: Path, dst: Path) -> None:
    for label, path in (("move", src), ("destination", dst)):
        if path == Path("/"):
            raise RelocateError(f"Refusing to use the filesystem root as the {label}.")
    if not src.is_dir() or src.is_symlink():
        raise RelocateError(f"{src} is not a folder.")
    reason = prefixmove._refusal_reason(src)  # noqa: SLF001 - same package, same rules
    if reason:
        raise RelocateError(f"Refusing to move {reason}.")
    real_src, real_dst = src.resolve(), dst.resolve()
    if real_dst == real_src or real_src in real_dst.parents:
        raise RelocateError("The new place is inside the folder being moved.")
    if real_dst in real_src.parents:
        raise RelocateError("The new place contains the folder being moved.")
    if dst.exists() or dst.is_symlink():
        raise RelocateError(f"{dst} already exists. Pick a place that does not exist yet.")


def _copy(
    src: Path,
    partial: Path,
    total: int,
    cancel: Cancel | None,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Copy ``src`` into ``partial``, an empty folder the caller just made."""
    done = 0
    dir_stats: list[tuple[Path, Path]] = [(src, partial)]
    for current, names, filenames in os.walk(src, followlinks=False, onerror=_walk_error):
        rel = Path(current).relative_to(src)
        target_dir = partial / rel
        for name in list(names):
            path = Path(current, name)
            if path.is_symlink():
                os.symlink(os.readlink(path), target_dir / name)
                names.remove(name)
            else:
                (target_dir / name).mkdir()
                dir_stats.append((path, target_dir / name))
        for name in filenames:
            path, target = Path(current, name), target_dir / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                os.symlink(os.readlink(path), target)
                continue
            if not stat.S_ISREG(mode):
                continue  # FIFO, socket or device: opening it could block forever
            with open(path, "rb") as reader, open(target, "wb") as writer:
                while chunk := reader.read(CHUNK):
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    writer.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            shutil.copystat(path, target)
    # Folder modes last: a read-only source folder must stay writable while it is filled.
    for source_dir, target_dir in reversed(dir_stats):
        shutil.copystat(source_dir, target_dir)
    if progress is not None:
        progress(total, total)


def _remove_partial(partial: Path) -> None:
    """Delete the copy this call made, including folders it already made read-only."""

    def make_writable(func, path, exc_info):  # noqa: ANN001 - shutil's callback signature
        try:
            os.chmod(os.path.dirname(path) or ".", 0o700)
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    shutil.rmtree(partial, onexc=make_writable)


def move_tree(
    src: Path,
    dst: Path,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Moved:
    """Move ``src`` to ``dst`` (which must not exist yet)."""
    src, dst = Path(src), Path(dst)
    _check(src, dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if _same_device(src, dst):
            if os.path.lexists(dst):
                raise RelocateError(f"{dst} appeared; nothing was replaced.")
            os.rename(src, dst)
            return Moved(renamed=True, kept_old=None)
    except OSError as exc:
        raise RelocateError(f"Could not move {src}: {exc}") from exc

    try:
        before = _summary(src)
    except OSError as exc:
        raise RelocateError(f"Could not read {src}: {exc}") from exc
    size = before[3]
    try:
        free = diskspace.free_bytes(dst.parent)
    except Exception as exc:  # noqa: BLE001 - BackendError/OSError: refuse, do not guess
        raise RelocateError(f"Could not read the free space at {dst.parent}: {exc}") from exc
    if free < size + SPACE_MARGIN:
        raise RelocateError(
            f"Not enough space at {dst.parent}: needs about "
            f"{diskspace.human_size(size + SPACE_MARGIN)}, "
            f"{diskspace.human_size(free)} free."
        )
    partial = dst.with_name(dst.name + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        raise RelocateError(f"{partial} is left over from an earlier move; remove it first.")
    created = False
    try:
        try:
            partial.mkdir()
        except FileExistsError:  # not ours: never clean it up
            raise RelocateError(f"{partial} appeared while starting; nothing changed.") from None
        created = True
        _copy(src, partial, size, cancel, progress)
        after = _summary(src)
        if after != before:
            raise RelocateError(
                "Files changed while they were being copied. Close the game and try again."
            )
        if _summary(partial)[:4] != before[:4]:
            raise RelocateError("The copy could not be verified against the original.")
        if os.path.lexists(dst):
            raise RelocateError(f"{dst} appeared while copying; nothing was replaced.")
        os.rename(partial, dst)
    except BaseException as exc:
        if created and partial.exists():
            _remove_partial(partial)  # only the copy this call made
        if isinstance(exc, OSError):
            raise RelocateError(f"Could not copy {src}: {exc}") from exc
        raise
    return Moved(renamed=False, kept_old=src)
