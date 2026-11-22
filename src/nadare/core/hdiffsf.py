# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Unpack an HDiffPatch single-stream directory diff made from an empty folder.

Duet Night Abyss publishes its whole PC client as one ``full_<version>.hdiff``: HDiffPatch's
directory diff (``HDIFF19`` head, :func:`nadare.core.krdiff.read_dir_head`) with a
single-stream body (``HDIFFSF20``, zstd), made against an empty folder. With nothing old to
copy from, the body is a list of covers that all have length 0, and every byte of the new files
comes straight out of the compressed stream, in the order the head lists the files. So a full
install needs no ``hpatchz.exe`` and no publisher launcher: it is a zstd stream cut into files.

Diffs that need old files (``old_size`` > 0, or any cover with a length) are refused: those are
updates, and they are not this module's job.

Read from HDiffPatch (MIT, ``libHDiffPatch/HPatch/patch.c``: ``getSingleCompressedDiffInfo``,
``_patch_single_stream_loop``, ``sspatch_covers_nextCover``) and checked against fixtures made by
its own ``hdiffz -SD -c-zstd``. Everything is bounded by the sizes the head declares; the stream
must end exactly where the head says, with no byte left over.
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import krdiff
from .events import Cancel
from .krdiff import FileEntry, read_dir_head

__all__ = ["HDiffError", "Header", "extract", "read_dir_head", "read_header"]

BLOCK = 1024 * 1024
#: HDiffPatch's own limit on a step buffer beyond the new data's size.
_STEP_SAFE_LIMIT = 1024 * 1024
MAX_STEP_BYTES = 64 * 1024 * 1024


class HDiffError(krdiff.KrDiffError):
    """A full-install diff that is damaged, hostile or needs old files."""


@dataclass(frozen=True)
class Header:
    files: tuple[FileEntry, ...]
    dirs: tuple[str, ...]
    new_size: int
    compression: str
    cover_count: int
    step_mem: int
    raw_size: int
    comp_size: int
    data_offset: int


def read_header(f: BinaryIO) -> Header:
    """Parse the ``HDIFF19`` head and the ``HDIFFSF20`` body header; no data is read."""
    try:
        head = read_dir_head(f, allow_old=False)
        if head.old_size or any(e.size for e in head.old_files):
            raise HDiffError("this diff needs old files; only a full install is supported")
        if krdiff._delim(f, ord("&"), 10) != "HDIFFSF20":
            raise HDiffError("not a single-stream diff (no HDIFFSF20)")
        compression = krdiff._delim(f, 0, 10)
        new_size, old_size, covers, step_mem, raw, comp = (krdiff._varint(f) for _ in range(6))
    except HDiffError:
        raise
    except krdiff.KrDiffError as exc:
        raise HDiffError(str(exc)) from exc
    if compression not in ("zstd", ""):
        raise HDiffError(f"unsupported compression {compression!r}")
    if old_size:
        raise HDiffError("this diff needs old files; only a full install is supported")
    if new_size != head.new_size:
        raise HDiffError("the diff body does not match its file list")
    if comp > raw or (compression and not comp) or (not compression and comp):
        raise HDiffError("the diff's stream sizes do not fit together")
    if step_mem > min(new_size, raw) + _STEP_SAFE_LIMIT or step_mem > MAX_STEP_BYTES:
        raise HDiffError("the diff's step buffer is larger than allowed")
    return Header(
        head.new_files, head.new_dirs, new_size, compression, covers, step_mem, raw, comp, f.tell()
    )


class _Stream:
    """The body's uncompressed stream, exactly ``size`` bytes, refused if it ends early."""

    def __init__(self, f: BinaryIO, header: Header) -> None:
        limited = krdiff._Limited(f, header.comp_size or header.raw_size)
        if header.comp_size:
            self._reader = krdiff._zstd().ZstdDecompressor().stream_reader(limited)
        else:
            self._reader = limited
        self.left = header.raw_size

    def read(self, n: int) -> bytes:
        if n > self.left:
            raise HDiffError("the diff's stream runs out")
        out = bytearray()
        while len(out) < n:
            try:
                chunk = self._reader.read(n - len(out))
            except Exception as exc:  # noqa: BLE001 - zstd raises its own error type
                raise HDiffError(f"the diff's stream is damaged: {exc}") from exc
            if not chunk:
                raise HDiffError("the diff's stream ends early")
            out += chunk
        self.left -= n
        return bytes(out)

    def at_end(self) -> bool:
        if self.left:
            return False
        try:
            return not self._reader.read(1)
        except Exception:  # noqa: BLE001
            return False


def _covers(buf: bytes, state: list[int]) -> list[tuple[int, int, int]]:
    """Decode one step's covers; ``state`` = [last old end, last new end, old pos, new pos, len]."""
    f = io.BytesIO(buf)
    out = []
    while f.tell() < len(buf):
        state[0], state[1] = state[2] + state[4], state[3] + state[4]
        first = krdiff._byte(f)
        delta = krdiff._varint(f, 1, first)
        state[2] = state[0] + delta if not first >> 7 else state[0] - delta
        state[3] = krdiff._varint(f) + state[1]
        state[4] = krdiff._varint(f)
        out.append((state[2], state[3], state[4]))
    return out


def extract(
    patch: str | os.PathLike[str],
    out_root: str | os.PathLike[str],
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Write the diff's files into ``out_root``, which must be an empty folder.

    Returns the relative paths of the files written. Raises :class:`HDiffError` when the diff
    is damaged or is not a full install; the caller checks the downloaded diff's own hash.
    """
    if not isinstance(patch, (str, os.PathLike)):
        raise TypeError("patch must be a path")
    patch, out_root = Path(patch), Path(out_root)
    if any(out_root.iterdir()):
        raise HDiffError(f"the output folder is not empty: {out_root}")
    with open(patch, "rb") as f:
        header = read_header(f)
        end = header.data_offset + (header.comp_size or header.raw_size)
        if os.fstat(f.fileno()).st_size != end:
            raise HDiffError("the diff's size does not match its header")
        f.seek(header.data_offset)
        stream = _Stream(f, header)

        for folder in header.dirs:
            (out_root / folder).mkdir(parents=True, exist_ok=True)
        index = -1
        handle: BinaryIO | None = None
        left = 0
        written = 0

        def open_next() -> None:
            nonlocal index, handle, left
            if handle is not None:
                handle.close()
            index += 1
            entry = header.files[index]
            target = out_root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            handle = os.fdopen(fd, "wb")
            left = entry.size

        def copy(amount: int) -> None:
            nonlocal written, left
            while amount:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                while left == 0:
                    if index + 1 >= len(header.files):
                        raise HDiffError("the diff writes past its files")
                    open_next()
                block = stream.read(min(amount, left, BLOCK))
                assert handle is not None
                handle.write(block)
                left -= len(block)
                amount -= len(block)
                written += len(block)
                if progress is not None:
                    progress(written, header.new_size)

        try:
            state = [0, 0, 0, 0, 0]
            remaining = header.cover_count
            while remaining:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                try:
                    cover_size = krdiff._varint(stream)  # type: ignore[arg-type]
                    rle_size = krdiff._varint(stream)  # type: ignore[arg-type]
                except krdiff.KrDiffError as exc:
                    raise HDiffError(str(exc)) from exc
                if cover_size + rle_size > header.step_mem:
                    raise HDiffError("a step is larger than the diff declares")
                step = stream.read(cover_size + rle_size)
                try:
                    covers = _covers(step[:cover_size], state)
                except krdiff.KrDiffError as exc:
                    raise HDiffError(str(exc)) from exc
                if not covers:
                    raise HDiffError("a step has no covers")
                for _old_pos, new_pos, length in covers:
                    if remaining <= 0:
                        raise HDiffError("the diff has more covers than it declares")
                    if new_pos < written or new_pos > header.new_size:
                        raise HDiffError("a cover points outside the new files")
                    copy(new_pos - written)
                    remaining -= 1
                    if length:
                        raise HDiffError(
                            "this diff needs old files; only a full install is supported"
                        )
                    if remaining:
                        raise HDiffError("an empty cover before the last one")
            if written != header.new_size or not stream.at_end():
                raise HDiffError("the diff's stream does not end where its files do")
            # files after the last byte written are the empty ones
            while index + 1 < len(header.files):
                open_next()
                if left:
                    raise HDiffError("the diff ends before its last file")
        finally:
            if handle is not None:
                handle.close()
        if progress is not None and header.new_size == 0:
            progress(0, 0)
    return [entry.path for entry in header.files]
