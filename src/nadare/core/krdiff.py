# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply Kuro's ``.krpdiff`` update patches (Wuthering Waves; #1252).

A ``.krpdiff`` is HDiffPatch's directory diff (``HDIFF19`` + an ``HDIFF13`` body) in Kuro's own
variant, which stock ``hpatchz`` refuses (``privateReservedDataSize``):

* the ``HDIFF19`` head lists the old files (concatenated into one "old" stream) and the new
  files (one "new" stream), with their sizes;
* the ``HDIFF13`` body is a list of *covers*. Each cover first takes ``new_pos_gap`` bytes from
  the (zstd-compressed) new-data stream, then copies ``length`` bytes of the old stream from a
  read position that is cumulative and wraps around the old stream's size. Unlike vanilla
  HDiff there is no byte-wise add (the RLE buffers are not used). Whatever the covers leave at
  the end comes from the new-data stream.

Read from TwintailTeam's MIT-licensed ``hdiffpatch-rs`` (``utils/patch/krdir.rs``) and checked
against five real 3.7.2 -> 3.8.0 groups, every output md5-exact.

Everything is streamed and bounded: no section is decompressed beyond the size its header
declares (and never beyond the module limits), every path stays inside its folder, and the
outputs go to a separate folder; callers verify them (Kuro's md5) before using them.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import paths
from .events import Cancel

#: Upper bounds a well-formed patch never reaches; anything bigger is refused.
MAX_PATHS = 200_000
MAX_HEAD_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 512 * 1024 * 1024
BLOCK = 1024 * 1024


class KrDiffError(ValueError):
    """A patch that is damaged, hostile or does not fit the files it is applied to."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int


@dataclass(frozen=True)
class Header:
    old_files: tuple[FileEntry, ...]
    new_files: tuple[FileEntry, ...]
    new_dirs: tuple[str, ...]
    old_size: int
    new_size: int
    cover_count: int
    cover_offset: int
    cover_size: int
    cover_comp_size: int
    data_offset: int
    data_size: int
    data_comp_size: int


# ---- low-level reading ------------------------------------------------------------------------


def _byte(f: BinaryIO) -> int:
    b = f.read(1)
    if not b:
        raise KrDiffError("the patch ends early")
    return b[0]


def _delim(f: BinaryIO, delim: int, limit: int) -> str:
    out = bytearray()
    while True:
        b = _byte(f)
        if b == delim:
            break
        out.append(b)
        if len(out) > limit:
            raise KrDiffError("a header field is too long")
    return out.decode("utf-8", "strict")


def _varint(f: BinaryIO, tag_bits: int = 0, first: int | None = None) -> int:
    """HDiffPatch's unsigned varint: big-endian 7-bit groups; ``tag_bits`` high bits of the
    first byte belong to the caller (a sign)."""
    code = _byte(f) if first is None else first
    width = 7 - tag_bits
    value = code & ((1 << width) - 1)
    if not code & (1 << width):
        return value
    for _ in range(10):
        code = _byte(f)
        value = (value << 7) | (code & 0x7F)
        if not code & 0x80:
            if value >= 1 << 63:
                break
            return value
    raise KrDiffError("a number in the patch is too large")


def _zstd():
    import zstandard

    return zstandard


def _section(f: BinaryIO, raw: int, comp: int, limit: int) -> io.BytesIO:
    if raw > limit or comp > limit:
        raise KrDiffError("a patch section is larger than allowed")
    body = f.read(comp if comp else raw)
    if len(body) != (comp if comp else raw):
        raise KrDiffError("the patch ends early")
    if not comp:
        return io.BytesIO(body)
    # Never more than the declared size (a tiny frame may claim gigabytes: #863).
    out = _zstd().ZstdDecompressor().stream_reader(io.BytesIO(body)).read(raw + 1)
    if len(out) != raw:
        raise KrDiffError("a patch section does not decompress to its declared size")
    return io.BytesIO(out)


def _files(paths_: list[str], offsets: list[int], sizes: list[int]) -> tuple[list, list]:
    """Split HDiffPatch's path list into files (the ``offsets`` say which) and folders."""
    files: list[FileEntry] = []
    dirs: list[str] = []
    want = offsets[0] if offsets else -1
    k = 0
    for i, path in enumerate(paths_):
        if i == want:
            files.append(FileEntry(path, sizes[len(files)]))
            k += 1
            want = want + offsets[k] + 1 if k < len(offsets) else -1
        else:
            dirs.append(path)
    if len(files) != len(sizes):
        raise KrDiffError("the patch's file list does not add up")
    return files, dirs


def _safe(path: str) -> str:
    try:
        return paths.check_relative(path.rstrip("/"), paths.SOPHON_PATHS)
    except paths.PathProblem as exc:
        raise KrDiffError(f"unsafe path in the patch: {path!r}") from exc


def read_header(f: BinaryIO) -> Header:
    """Parse the ``HDIFF19`` head and the ``HDIFF13`` body's header (no data is read)."""
    if _delim(f, ord("&"), 10) != "HDIFF19":
        raise KrDiffError("not a Kuro directory patch (no HDIFF19)")
    compression = _delim(f, ord("&"), 10)
    _delim(f, 0, 15)  # checksum name; Kuro's md5 list is what outputs are checked against
    if compression not in ("zstd", ""):
        raise KrDiffError(f"unsupported patch compression {compression!r}")
    _byte(f)
    _byte(f)
    fields = [_varint(f) for _ in range(17)]
    (old_count, _, new_count, _, old_refs, old_size, new_refs, new_size) = fields[:8]
    private_extern, extern, head_raw, head_comp, checksum_bytes = fields[12:17]
    if max(old_count, new_count, old_refs, new_refs) > MAX_PATHS:
        raise KrDiffError("the patch lists too many files")
    f.seek(checksum_bytes * 4, os.SEEK_CUR)
    head = _section(f, head_raw, head_comp, MAX_HEAD_BYTES)
    old_paths = [_delim(head, 0, 1024) for _ in range(old_count)]
    new_paths = [_delim(head, 0, 1024) for _ in range(new_count)]
    old_offsets = [_varint(head) for _ in range(old_refs)]
    new_offsets = [_varint(head) for _ in range(new_refs)]
    old_sizes = [_varint(head) for _ in range(old_refs)]
    new_sizes = [_varint(head) for _ in range(new_refs)]
    old_files, _ = _files(old_paths, old_offsets, old_sizes)
    new_files, new_dirs = _files(new_paths, new_offsets, new_sizes)
    if sum(e.size for e in old_files) != old_size or sum(e.size for e in new_files) != new_size:
        raise KrDiffError("the patch's file sizes do not add up")
    old_files = [FileEntry(_safe(e.path), e.size) for e in old_files]
    new_files = [FileEntry(_safe(e.path), e.size) for e in new_files]
    new_dirs = [_safe(d) for d in new_dirs if d.strip("/")]
    f.seek(private_extern + extern, os.SEEK_CUR)

    if _delim(f, ord("&"), 10) != "HDIFF13":
        raise KrDiffError("not a Kuro directory patch (no HDIFF13)")
    _delim(f, 0, 10)
    (data_new, data_old, covers, cover_raw, cover_comp, ctrl_raw, ctrl_comp, code_raw,
     code_comp, diff_raw, diff_comp) = (_varint(f) for _ in range(11))
    if data_new != new_size or data_old != old_size:
        raise KrDiffError("the patch body does not match its file list")
    if cover_raw > MAX_COVER_BYTES or cover_comp > MAX_COVER_BYTES:
        raise KrDiffError("the patch's cover list is larger than allowed")
    if diff_raw > new_size:
        raise KrDiffError("the patch carries more new data than its new files hold")
    cover_offset = f.tell()
    data_offset = cover_offset + (cover_comp or cover_raw) + (ctrl_comp or ctrl_raw) + (
        code_comp or code_raw
    )
    return Header(
        tuple(old_files), tuple(new_files), tuple(new_dirs), old_size, new_size,
        covers, cover_offset, cover_raw, cover_comp, data_offset, diff_raw, diff_comp,
    )


def _covers(f: BinaryIO, header: Header) -> Iterator[tuple[int, int, int]]:
    f.seek(header.cover_offset)
    body = _section(f, header.cover_size, header.cover_comp_size, MAX_COVER_BYTES)
    for _ in range(header.cover_count):
        first = _byte(body)
        magnitude = _varint(body, 1, first)
        delta = -magnitude if first & 0x80 else magnitude
        yield delta, _varint(body), _varint(body)


class _Exact:
    """Read exactly ``size`` bytes of a (maybe zstd-compressed) slice of the patch file."""

    def __init__(self, path: Path, offset: int, size: int, comp: int) -> None:
        self._file = open(path, "rb")  # noqa: SIM115 - closed by close()
        self._file.seek(offset)
        self._left = size
        if comp:
            limited = _Limited(self._file, comp)
            self._reader = _zstd().ZstdDecompressor().stream_reader(limited)
        else:
            self._reader = _Limited(self._file, size)

    def read(self, n: int) -> bytes:
        if n > self._left:
            raise KrDiffError("the patch's new data runs out")
        out = bytearray()
        while len(out) < n:
            chunk = self._reader.read(n - len(out))
            if not chunk:
                raise KrDiffError("the patch's new data runs out")
            out += chunk
        self._left -= n
        return bytes(out)

    def close(self) -> None:
        self._file.close()


class _Limited(io.RawIOBase):
    def __init__(self, f: BinaryIO, size: int) -> None:
        self._f, self._left = f, size

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:  # noqa: ANN001 - io protocol
        n = min(len(buf), self._left)
        if n <= 0:
            return 0
        data = self._f.read(n)
        buf[: len(data)] = data
        self._left -= len(data)
        return len(data)


def _open_plain(path: Path) -> BinaryIO:
    """Open a regular file for reading without following a link at its last part."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise KrDiffError(f"{path} is not a regular file")
    return handle


def apply(
    patch: Path | str,
    old_root: Path | str,
    out_root: Path | str,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Build the patch's new files under ``out_root`` from the old files under ``old_root``.

    Returns ``{relative path: md5}`` of every new file. ``out_root`` must be a separate folder;
    nothing under ``old_root`` is written. Raises :class:`KrDiffError` when the patch is damaged
    or the old files are not the ones it was made for (size check; the caller checks md5).
    """
    patch, old_root, out_root = Path(patch), Path(old_root), Path(out_root)
    with open(patch, "rb") as f:
        header = read_header(f)
    olds: list[tuple[int, int, BinaryIO]] = []
    outs: list[tuple[FileEntry, BinaryIO, hashlib._Hash]] = []
    data = None
    try:
        start = 0
        for entry in header.old_files:
            handle = _open_plain(old_root / entry.path)
            if os.fstat(handle.fileno()).st_size != entry.size:
                handle.close()
                raise KrDiffError(f"{entry.path} is not the file this patch was made for")
            olds.append((start, start + entry.size, handle))
            start += entry.size
        for folder in header.new_dirs:
            (out_root / folder).mkdir(parents=True, exist_ok=True)
        for entry in header.new_files:
            target = out_root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            outs.append((entry, os.fdopen(fd, "wb"), hashlib.md5()))
        data = _Exact(patch, header.data_offset, header.data_size, header.data_comp_size)

        written = 0
        current = 0
        left = outs[0][0].size if outs else 0

        def write(block: bytes) -> None:
            nonlocal written, current, left
            while block:
                while left == 0:
                    current += 1
                    if current >= len(outs):
                        raise KrDiffError("the patch writes past its new files")
                    left = outs[current][0].size
                take = min(left, len(block))
                _, handle, digest = outs[current]
                handle.write(block[:take])
                digest.update(block[:take])
                left -= take
                written += take
                block = block[take:]
            if progress is not None:
                progress(written, header.new_size)

        def copy_old(at: int, n: int) -> None:
            while n:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                for begin, end, handle in olds:
                    if begin <= at < end:
                        handle.seek(at - begin)
                        chunk = handle.read(min(n, end - at, BLOCK))
                        break
                else:
                    raise KrDiffError("the patch reads outside the old files")
                if not chunk:
                    raise KrDiffError("an old file is shorter than the patch expects")
                write(chunk)
                at += len(chunk)
                n -= len(chunk)

        def copy_new(n: int) -> None:
            while n:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                take = min(n, BLOCK)
                write(data.read(take))
                n -= take

        with open(patch, "rb") as f:
            position = 0
            for delta, gap, length in _covers(f, header):
                position += delta
                if header.old_size:
                    position %= header.old_size  # cumulative and wrapping (Kuro's variant)
                if written + gap + length > header.new_size:
                    raise KrDiffError("the patch writes past its new files")
                copy_new(gap)
                if length:
                    if position + length > header.old_size:
                        raise KrDiffError("the patch reads outside the old files")
                    copy_old(position, length)
                position += length
        copy_new(header.new_size - written)
        if written != header.new_size:
            raise KrDiffError("the patch did not produce its new files")
    finally:
        for _, _, handle in olds:
            handle.close()
        for _, handle, _ in outs:
            handle.close()
        if data is not None:
            data.close()
    return {entry.path: digest.hexdigest() for entry, _, digest in outs}


__all__ = ["FileEntry", "Header", "KrDiffError", "apply", "read_header"]
