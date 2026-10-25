# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A small, streaming 7z reader built on the standard ``lzma`` module.

Nadare ships no archive library, and some publishers (HoYo's Honkai titles) ship
the game as a 7z. This reads the container (signature header, optionally
LZMA-compressed header, pack/unpack/substream info, file names and attributes)
and decodes the common coder chains (copy, LZMA, LZMA2, BCJ x86, delta, ARM and
friends) one chunk at a time, so a 30 GB archive never sits in memory.

Not supported (raises :class:`SevenZipError`): encryption (AES), PPMd, BZip2,
Deflate and other exotic coders, multi-input coders, and symlinks.

Every member path is checked before anything is written; each file's CRC-32 is
verified as it is written, and a file that fails is removed.
"""

from __future__ import annotations

import lzma
import stat
import struct
import threading
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .events import Cancel, Cancelled


class SevenZipError(ValueError):
    """The archive is damaged, unsupported, or would write outside its destination."""


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    is_dir: bool
    #: CRC-32 of the file, when the archive records one (none for empty files).
    crc: int | None = None
    #: Index of the solid block the file is stored in (none for empty files and directories).
    folder: int | None = None


_MAGIC = b"7z\xbc\xaf\x27\x1c"
_MAX_HEADER = 64 * 1024 * 1024
_MAX_FILES = 10_000_000
_CHUNK = 1 << 20


@dataclass
class _Coder:
    id: bytes
    props: bytes
    num_in: int
    num_out: int


@dataclass
class _Folder:
    coders: list[_Coder]
    bind_pairs: list[tuple[int, int]]
    packed_indices: list[int]
    unpack_sizes: list[int] = field(default_factory=list)
    digest: int | None = None
    num_streams: int = 1
    stream_sizes: list[int] = field(default_factory=list)
    stream_digests: list[int | None] = field(default_factory=list)
    packed_start: int = 0
    decode_order: list[int] = field(default_factory=list)
    main_out: int = 0
    unpack_size: int = 0
    filters: list[dict[str, int]] = field(default_factory=list)


@dataclass
class _File:
    path: str
    size: int
    is_dir: bool
    attrs: int | None
    folder: int | None
    stream: int | None
    digest: int | None
    empty: bool


@dataclass
class _Archive:
    files: list[_File]
    folders: list[_Folder]
    pack_pos: int
    pack_sizes: list[int]


@dataclass
class _StreamsInfo:
    pack_pos: int
    pack_sizes: list[int]
    folders: list[_Folder]


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise SevenZipError("truncated header")
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def byte(self) -> int:
        return self.read(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def number(self) -> int:
        return _read_number(self)

    def remaining(self) -> bytes:
        return self.read(len(self.data) - self.pos)


def _crc32(data: bytes | bytearray) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _read_number(r: _Reader) -> int:
    first = r.byte()
    mask = 0x80
    value = 0
    for i in range(8):
        if first & mask == 0:
            value += (first & (mask - 1)) << (8 * i)
            return value
        value |= r.byte() << (8 * i)
        mask >>= 1
    return value


def _read_bits(r: _Reader, count: int) -> list[bool]:
    raw = r.read((count + 7) // 8)
    return [bool(raw[i // 8] & (0x80 >> (i % 8))) for i in range(count)]


def _read_digests(r: _Reader, count: int) -> list[int | None]:
    defined = r.byte()
    bits = _read_bits(r, count) if defined == 0 else [True] * count
    return [r.u32() if bit else None for bit in bits]


def _coder_filter(coder: _Coder) -> dict[str, int] | None:
    cid = coder.id
    if cid == b"\x00":
        return None
    if cid == b"\x21":
        if len(coder.props) != 1:
            raise SevenZipError("bad LZMA2 properties")
        prop = coder.props[0]
        if prop > 40:
            raise SevenZipError("bad LZMA2 properties")
        dict_size = 0xFFFFFFFF if prop == 40 else (2 | (prop & 1)) << (prop // 2 + 11)
        return {"id": lzma.FILTER_LZMA2, "dict_size": dict_size}
    if cid == b"\x03\x01\x01":
        if len(coder.props) != 5:
            raise SevenZipError("bad LZMA properties")
        first = coder.props[0]
        lc = first % 9
        first //= 9
        lp = first % 5
        pb = first // 5
        dict_size = int.from_bytes(coder.props[1:5], "little")
        return {
            "id": lzma.FILTER_LZMA1,
            "dict_size": dict_size,
            "lc": lc,
            "lp": lp,
            "pb": pb,
        }
    if cid == b"\x03\x03\x01\x03":
        return {"id": lzma.FILTER_X86}
    if cid == b"\x03":
        if not coder.props:
            raise SevenZipError("bad Delta properties")
        return {"id": lzma.FILTER_DELTA, "dist": coder.props[0] + 1}
    names = {
        b"\x03\x03\x05\x01": "FILTER_ARM",
        b"\x03\x03\x07\x01": "FILTER_ARMTHUMB",
        b"\x03\x03\x02\x05": "FILTER_POWERPC",
        b"\x03\x03\x04\x01": "FILTER_IA64",
        b"\x03\x03\x08\x05": "FILTER_SPARC",
    }
    name = names.get(cid)
    if name is not None:
        constant = getattr(lzma, name, None)
        if constant is not None:
            return {"id": constant}
    raise SevenZipError(f"unsupported coder {cid.hex()}")


def _analyse_folder(folder: _Folder) -> None:
    if not folder.coders:
        raise SevenZipError("folder has no coders")
    if any(coder.num_in != 1 or coder.num_out != 1 for coder in folder.coders):
        raise SevenZipError("unsupported multi-input coder")
    if len(folder.packed_indices) != 1:
        raise SevenZipError("unsupported multi-stream folder")
    count = len(folder.coders)
    if len(folder.unpack_sizes) != count:
        raise SevenZipError("bad folder unpack sizes")
    in_index = folder.packed_indices[0]
    if not 0 <= in_index < count:
        raise SevenZipError("bad packed stream index")
    out_index = in_index
    by_out: dict[int, int] = {}
    for pair_in, pair_out in folder.bind_pairs:
        if not 0 <= pair_in < count or not 0 <= pair_out < count or pair_out in by_out:
            raise SevenZipError("bad bind pair")
        by_out[pair_out] = pair_in
    order: list[int] = []
    seen: set[int] = set()
    while True:
        if out_index in seen:
            raise SevenZipError("cyclic coder graph")
        seen.add(out_index)
        order.append(out_index)
        if out_index not in by_out:
            break
        in_index = by_out[out_index]
        out_index = in_index
    if len(order) != count:
        raise SevenZipError("incomplete coder graph")
    folder.decode_order = order
    folder.main_out = out_index
    folder.unpack_size = folder.unpack_sizes[out_index]
    filters: list[dict[str, int]] = []
    for index in reversed(order):
        filt = _coder_filter(folder.coders[index])
        if filt is not None:
            filters.append(filt)
    folder.filters = filters


def _parse_folder(r: _Reader) -> _Folder:
    num_coders = r.number()
    if num_coders <= 0 or num_coders > _MAX_FILES:
        raise SevenZipError("bad coder count")
    coders: list[_Coder] = []
    for _ in range(num_coders):
        flag = r.byte()
        if flag & 0x80:
            raise SevenZipError("unsupported coder flag")
        id_size = flag & 0x0F
        complex_coder = bool(flag & 0x10)
        has_props = bool(flag & 0x20)
        if id_size == 0:
            raise SevenZipError("empty coder id")
        cid = r.read(id_size)
        if complex_coder:
            num_in = r.number()
            num_out = r.number()
        else:
            num_in = num_out = 1
        props = b""
        if has_props:
            props = r.read(r.number())
        coders.append(_Coder(cid, props, num_in, num_out))
    total_in = sum(coder.num_in for coder in coders)
    total_out = sum(coder.num_out for coder in coders)
    num_bind_pairs = total_out - 1
    if num_bind_pairs < 0:
        raise SevenZipError("bad bind pair count")
    bind_pairs: list[tuple[int, int]] = []
    for _ in range(num_bind_pairs):
        bind_pairs.append((r.number(), r.number()))
    num_packed = total_in - num_bind_pairs
    if num_packed < 0:
        raise SevenZipError("bad packed stream count")
    if num_packed == 1:
        used = {pair_in for pair_in, _ in bind_pairs}
        candidates = [index for index in range(total_in) if index not in used]
        if len(candidates) != 1:
            raise SevenZipError("bad packed stream index")
        packed_indices = candidates
    else:
        packed_indices = [r.number() for _ in range(num_packed)]
    return _Folder(coders, bind_pairs, packed_indices)


def _parse_pack_info(r: _Reader) -> tuple[int, list[int]]:
    pack_pos = r.number()
    num_pack_streams = r.number()
    if num_pack_streams > _MAX_FILES:
        raise SevenZipError("too many pack streams")
    sizes: list[int] = []
    while True:
        prop = r.number()
        if prop == 0:
            break
        if prop == 0x09:
            sizes = [r.number() for _ in range(num_pack_streams)]
        elif prop == 0x0A:
            _read_digests(r, num_pack_streams)
        else:
            raise SevenZipError(f"unsupported PackInfo property {prop:#x}")
    if len(sizes) != num_pack_streams:
        raise SevenZipError("missing pack sizes")
    return pack_pos, sizes


def _parse_unpack_info(r: _Reader) -> list[_Folder]:
    if r.byte() != 0x0B:
        raise SevenZipError("missing folder list")
    num_folders = r.number()
    if num_folders > _MAX_FILES:
        raise SevenZipError("too many folders")
    if r.byte() != 0:
        raise SevenZipError("external folders are not supported")
    folders = [_parse_folder(r) for _ in range(num_folders)]
    if r.byte() != 0x0C:
        raise SevenZipError("missing unpack sizes")
    for folder in folders:
        total_out = sum(coder.num_out for coder in folder.coders)
        folder.unpack_sizes = [r.number() for _ in range(total_out)]
    for folder in folders:
        _analyse_folder(folder)
    prop = r.byte()
    if prop == 0x0A:
        digests = _read_digests(r, num_folders)
        for folder, digest in zip(folders, digests, strict=True):
            folder.digest = digest
        prop = r.byte()
    if prop != 0:
        raise SevenZipError(f"unsupported UnPackInfo property {prop:#x}")
    return folders


def _default_substreams(folders: list[_Folder]) -> None:
    for folder in folders:
        folder.num_streams = 1
        folder.stream_sizes = [folder.unpack_size]
        folder.stream_digests = [folder.digest]


def _parse_substreams_info(r: _Reader, folders: list[_Folder]) -> None:
    counts = [1] * len(folders)
    sizes: list[list[int]] | None = None
    crc_values: list[int | None] | None = None
    while True:
        prop = r.number()
        if prop == 0:
            break
        if prop == 0x0D:
            counts = [r.number() for _ in folders]
        elif prop == 0x09:
            sizes = []
            for folder, count in zip(folders, counts, strict=True):
                if count == 0:
                    sizes.append([])
                    continue
                values = [r.number() for _ in range(count - 1)]
                last = folder.unpack_size - sum(values)
                if last < 0:
                    raise SevenZipError("bad substream sizes")
                values.append(last)
                sizes.append(values)
        elif prop == 0x0A:
            unknown = sum(
                count
                for folder, count in zip(folders, counts, strict=True)
                if not (count == 1 and folder.digest is not None)
            )
            crc_values = _read_digests(r, unknown)
        else:
            raise SevenZipError(f"unsupported SubStreamsInfo property {prop:#x}")
    if sizes is None:
        sizes = []
        for folder, count in zip(folders, counts, strict=True):
            if count == 0:
                sizes.append([])
            elif count == 1:
                sizes.append([folder.unpack_size])
            else:
                raise SevenZipError("missing substream sizes")
    for folder, count, stream_sizes in zip(folders, counts, sizes, strict=True):
        if len(stream_sizes) != count or sum(stream_sizes) != folder.unpack_size:
            raise SevenZipError("bad substream layout")
        folder.num_streams = count
        folder.stream_sizes = stream_sizes
    if crc_values is None:
        for folder in folders:
            folder.stream_digests = (
                [folder.digest] if folder.num_streams == 1 else [None] * folder.num_streams
            )
        return
    index = 0
    for folder in folders:
        digests: list[int | None] = []
        for _ in range(folder.num_streams):
            if folder.num_streams == 1 and folder.digest is not None:
                digests.append(folder.digest)
            else:
                digests.append(crc_values[index])
                index += 1
        folder.stream_digests = digests
    if index != len(crc_values):
        raise SevenZipError("bad substream digest count")


def _parse_streams_info(r: _Reader) -> _StreamsInfo:
    pack_pos = 0
    pack_sizes: list[int] = []
    folders: list[_Folder] = []
    has_substreams = False
    while True:
        prop = r.number()
        if prop == 0:
            break
        if prop == 0x06:
            pack_pos, pack_sizes = _parse_pack_info(r)
        elif prop == 0x07:
            folders = _parse_unpack_info(r)
        elif prop == 0x08:
            _parse_substreams_info(r, folders)
            has_substreams = True
        else:
            raise SevenZipError(f"unsupported StreamsInfo property {prop:#x}")
    cursor = 0
    for folder in folders:
        folder.packed_start = cursor
        cursor += len(folder.packed_indices)
    if folders and cursor != len(pack_sizes):
        raise SevenZipError("pack stream count mismatch")
    if not has_substreams:
        _default_substreams(folders)
    return _StreamsInfo(pack_pos, pack_sizes, folders)


def _parse_files_info(r: _Reader) -> tuple[list[bool], list[bool], list[str], list[int | None]]:
    num_files = r.number()
    if num_files > _MAX_FILES:
        raise SevenZipError("too many files")
    props: dict[int, bytes] = {}
    while True:
        prop = r.number()
        if prop == 0:
            break
        props[prop] = r.read(r.number())
    if 0x0E in props:
        empty_stream = _read_bits(_Reader(props[0x0E]), num_files)
    else:
        empty_stream = [False] * num_files
    empty_count = sum(empty_stream)
    if 0x0F in props:
        empty_file = _read_bits(_Reader(props[0x0F]), empty_count)
    else:
        empty_file = [False] * empty_count
    if 0x11 not in props:
        raise SevenZipError("missing file names")
    names_reader = _Reader(props[0x11])
    if names_reader.byte() != 0:
        raise SevenZipError("external names are not supported")
    text = names_reader.remaining().decode("utf-16-le")
    parts = text.split("\x00")
    if len(parts) < num_files:
        raise SevenZipError("bad file name list")
    names = parts[:num_files]
    attrs: list[int | None] = [None] * num_files
    if 0x15 in props:
        attr_reader = _Reader(props[0x15])
        defined = (
            _read_bits(attr_reader, num_files) if attr_reader.byte() == 0 else [True] * num_files
        )
        if attr_reader.byte() != 0:
            raise SevenZipError("external attributes are not supported")
        for index, is_defined in enumerate(defined):
            if is_defined:
                attrs[index] = attr_reader.u32()
    return empty_stream, empty_file, names, attrs


def _safe_name(raw: str) -> str:
    if "\x00" in raw:
        raise SevenZipError("file name contains NUL")
    name = raw.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/"):
        raise SevenZipError(f"unsafe file name {raw!r}")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise SevenZipError(f"unsafe file name {raw!r}")
    parts = name.split("/")
    if any(part == ".." for part in parts):
        raise SevenZipError(f"unsafe file name {raw!r}")
    clean = "/".join(part for part in parts if part not in ("", "."))
    if not clean:
        raise SevenZipError(f"unsafe file name {raw!r}")
    return clean


def _check_attrs(path: str, attrs: int | None, is_dir: bool) -> None:
    if attrs is None or not (attrs & 0x8000):
        return
    mode = (attrs >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise SevenZipError(f"symlink not supported: {path}")
    if is_dir:
        if not stat.S_ISDIR(mode):
            raise SevenZipError(f"non-directory mode on directory: {path}")
    elif not stat.S_ISREG(mode):
        raise SevenZipError(f"non-regular file not supported: {path}")


def _build_files(
    folders: list[_Folder],
    empty_stream: list[bool],
    empty_file: list[bool],
    names: list[str],
    attrs: list[int | None],
) -> list[_File]:
    streams: list[tuple[int, int, int, int | None]] = []
    for folder_index, folder in enumerate(folders):
        for stream_index in range(folder.num_streams):
            streams.append(
                (
                    folder_index,
                    stream_index,
                    folder.stream_sizes[stream_index],
                    folder.stream_digests[stream_index],
                )
            )
    files: list[_File] = []
    empty_index = 0
    stream_index = 0
    for index, is_empty in enumerate(empty_stream):
        path = _safe_name(names[index])
        if is_empty:
            is_file = empty_file[empty_index]
            empty_index += 1
            is_dir = not is_file
            _check_attrs(path, attrs[index], is_dir)
            files.append(_File(path, 0, is_dir, attrs[index], None, None, None, is_file))
            continue
        if stream_index >= len(streams):
            raise SevenZipError("not enough substreams")
        folder_index, folder_stream, size, digest = streams[stream_index]
        stream_index += 1
        _check_attrs(path, attrs[index], False)
        files.append(
            _File(path, size, False, attrs[index], folder_index, folder_stream, digest, False)
        )
    if stream_index != len(streams):
        raise SevenZipError("too many substreams")
    return files


def _parse_header_body(header: bytes) -> _Archive:
    r = _Reader(header)
    if r.byte() != 0x01:
        raise SevenZipError("not a 7z header")
    streams = _StreamsInfo(0, [], [])
    files_info: tuple[list[bool], list[bool], list[str], list[int | None]] | None = None
    while True:
        prop = r.number()
        if prop == 0:
            break
        if prop == 0x02:
            while True:
                kind = r.number()
                if kind == 0:
                    break
                r.read(r.number())
        elif prop == 0x03:
            raise SevenZipError("additional streams are not supported")
        elif prop == 0x04:
            streams = _parse_streams_info(r)
        elif prop == 0x05:
            files_info = _parse_files_info(r)
        else:
            raise SevenZipError(f"unsupported header property {prop:#x}")
    if files_info is None:
        return _Archive([], streams.folders, streams.pack_pos, streams.pack_sizes)
    empty_stream, empty_file, names, attrs = files_info
    files = _build_files(streams.folders, empty_stream, empty_file, names, attrs)
    return _Archive(files, streams.folders, streams.pack_pos, streams.pack_sizes)


def _pack_extent(streams: _StreamsInfo, folder: _Folder) -> tuple[int, int]:
    start = folder.packed_start
    count = len(folder.packed_indices)
    if start + count > len(streams.pack_sizes):
        raise SevenZipError("missing packed stream")
    offset = 32 + streams.pack_pos + sum(streams.pack_sizes[:start])
    size = sum(streams.pack_sizes[start : start + count])
    return offset, size


def _iter_folder(
    fh: BinaryIO, folder: _Folder, offset: int, pack_size: int, limit: int
) -> Iterator[bytes]:
    if limit <= 0:
        return
    if not folder.filters:
        fh.seek(offset)
        remaining = min(pack_size, limit)
        produced = 0
        while produced < limit:
            if remaining <= 0:
                raise SevenZipError("truncated packed stream")
            chunk = fh.read(min(_CHUNK, remaining))
            if not chunk:
                raise SevenZipError("truncated packed stream")
            remaining -= len(chunk)
            produced += len(chunk)
            yield chunk
        return
    fh.seek(offset)
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=folder.filters)
    remaining_in = pack_size
    produced = 0
    while True:
        if decompressor.needs_input:
            if remaining_in <= 0:
                raise SevenZipError("truncated packed stream")
            data = fh.read(min(_CHUNK, remaining_in))
            if not data:
                raise SevenZipError("truncated packed stream")
            remaining_in -= len(data)
        else:
            data = b""
        out = decompressor.decompress(data, max_length=_CHUNK)
        if out:
            if produced + len(out) >= limit:
                yield out[: limit - produced]
                return
            produced += len(out)
            yield out
        if decompressor.eof:
            if produced < limit:
                raise SevenZipError("unpacked stream ended early")
            return
        if not out and decompressor.needs_input and remaining_in == 0:
            raise SevenZipError("truncated packed stream")


def _decode_header(fh: BinaryIO, streams: _StreamsInfo, folder: _Folder) -> bytes:
    if folder.unpack_size > _MAX_HEADER:
        raise SevenZipError("decoded header is too large")
    offset, pack_size = _pack_extent(streams, folder)
    data = bytearray()
    for chunk in _iter_folder(fh, folder, offset, pack_size, folder.unpack_size):
        data.extend(chunk)
        if len(data) > _MAX_HEADER:
            raise SevenZipError("decoded header is too large")
    if len(data) != folder.unpack_size:
        raise SevenZipError("header size mismatch")
    if folder.digest is not None and _crc32(data) != folder.digest:
        raise SevenZipError("header CRC mismatch")
    return bytes(data)


def _read_next_header(fh: BinaryIO) -> bytes:
    fh.seek(0)
    head = fh.read(32)
    if len(head) != 32 or head[:6] != _MAGIC:
        raise SevenZipError("not a 7z archive")
    if head[6] != 0:
        raise SevenZipError("unsupported 7z version")
    start_crc = struct.unpack_from("<I", head, 8)[0]
    if _crc32(head[12:32]) != start_crc:
        raise SevenZipError("signature CRC mismatch")
    offset, size, header_crc = struct.unpack_from("<QQI", head, 12)
    if size == 0:
        return b""
    if size > _MAX_HEADER:
        raise SevenZipError("next header is too large")
    fh.seek(0, 2)
    file_size = fh.tell()
    start = 32 + offset
    if start < 32 or start + size > file_size:
        raise SevenZipError("next header is outside the archive")
    fh.seek(start)
    header = fh.read(size)
    if len(header) != size:
        raise SevenZipError("truncated header")
    if _crc32(header) != header_crc:
        raise SevenZipError("header CRC mismatch")
    return header


def _parse_archive(fh: BinaryIO) -> _Archive:
    header = _read_next_header(fh)
    if not header:
        return _Archive([], [], 0, [])
    for _ in range(4):
        if header[:1] != b"\x17":
            return _parse_header_body(header)
        r = _Reader(header)
        r.byte()
        streams = _parse_streams_info(r)
        if not streams.folders:
            raise SevenZipError("encoded header has no folder")
        header = _decode_header(fh, streams, streams.folders[0])
        if not header:
            raise SevenZipError("empty encoded header")
    raise SevenZipError("too many encoded header levels")


def list_entries(fh: BinaryIO) -> list[Entry]:
    """Parse the archive's headers and list its members (directories included).

    ``fh`` is any seekable binary file object, e.g. an open file or a joined
    multi-volume reader. Raises :class:`SevenZipError` for anything that is not a
    well-formed supported 7z.
    """
    try:
        archive = _parse_archive(fh)
        return [
            Entry(item.path, item.size, item.is_dir, item.digest, item.folder)
            for item in archive.files
        ]
    except SevenZipError:
        raise
    except (IndexError, struct.error, ValueError, EOFError, lzma.LZMAError, UnicodeDecodeError) as exc:
        raise SevenZipError(str(exc)) from exc


def _extract_archive(
    fh: BinaryIO,
    dest: Path,
    only: Callable[[str], bool] | None,
    cancel: Cancel | None,
    progress: Callable[[int, int], None] | None,
    workers: int = 1,
    open_reader: Callable[[], BinaryIO] | None = None,
) -> int:
    archive = _parse_archive(fh)
    if cancel is not None:
        cancel.raise_if_cancelled()

    def wanted(path: str) -> bool:
        return only is None or only(path)

    dirs = [item for item in archive.files if item.is_dir and wanted(item.path)]
    empty_files = [item for item in archive.files if item.empty and not item.is_dir and wanted(item.path)]
    regular = [item for item in archive.files if not item.is_dir and not item.empty and wanted(item.path)]

    by_folder: dict[int, list[_File]] = {}
    for item in regular:
        assert item.folder is not None
        by_folder.setdefault(item.folder, []).append(item)

    plan: dict[int, tuple[dict[int, _File], int, int]] = {}
    total = 0
    for folder_index, items in by_folder.items():
        folder = archive.folders[folder_index]
        stream_map = {item.stream: item for item in items if item.stream is not None}
        last_stream = max(stream_map)
        prefix = sum(folder.stream_sizes[: last_stream + 1])
        plan[folder_index] = (stream_map, last_stream, prefix)
        total += prefix

    dest.mkdir(parents=True, exist_ok=True)
    for item in dirs:
        (dest / item.path).mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()
    count = 0
    done = 0
    #: Set when one worker fails, so the others stop between chunks instead of finishing a block.
    internal = Cancel()

    def add_count(n: int) -> None:
        nonlocal count
        with lock:
            count += n

    def add_done(n: int) -> None:
        nonlocal done
        with lock:
            done += n

    def report() -> None:
        if progress is None:
            return
        with lock:
            progress(done, total)

    for item in empty_files:
        out = dest / item.path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        add_count(1)

    def finish_zero_streams(
        folder: _Folder,
        stream_map: dict[int, _File],
        current: int,
        last_stream: int,
        dest_dir: Path,
        stream_pos: int = 0,
    ) -> tuple[int, int]:
        start = current
        while current <= last_stream and folder.stream_sizes[current] == 0:
            record = stream_map.get(current)
            if record is not None:
                out = dest_dir / record.path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"")
                if record.digest is not None and record.digest != 0:
                    out.unlink(missing_ok=True)
                    raise SevenZipError(f"CRC mismatch for {record.path}")
                add_count(1)
            current += 1
        # Keep the position inside a stream that is still being written.
        return current, (stream_pos if current == start else 0)

    def extract_folder(reader: BinaryIO, folder_index: int) -> None:
        folder = archive.folders[folder_index]
        stream_map, last_stream, prefix = plan[folder_index]
        streams = _StreamsInfo(archive.pack_pos, archive.pack_sizes, archive.folders)
        offset, pack_size = _pack_extent(streams, folder)
        current = 0
        stream_pos = 0
        active: Path | None = None
        out_file = None
        running_crc = 0
        try:
            current, stream_pos = finish_zero_streams(
                folder, stream_map, current, last_stream, dest
            )
            for chunk in _iter_folder(reader, folder, offset, pack_size, prefix):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                if internal.cancelled:
                    raise Cancelled()
                pos = 0
                while pos < len(chunk) and current <= last_stream:
                    current, stream_pos = finish_zero_streams(
                        folder, stream_map, current, last_stream, dest, stream_pos
                    )
                    if current > last_stream:
                        break
                    room = folder.stream_sizes[current] - stream_pos
                    take = min(len(chunk) - pos, room)
                    if take <= 0:
                        break
                    piece = chunk[pos : pos + take]
                    record = stream_map.get(current)
                    if record is not None:
                        if out_file is None:
                            active = dest / record.path
                            active.parent.mkdir(parents=True, exist_ok=True)
                            out_file = open(active, "wb")
                            running_crc = 0
                        out_file.write(piece)
                        running_crc = zlib.crc32(piece, running_crc) & 0xFFFFFFFF
                    pos += take
                    stream_pos += take
                    add_done(take)
                    if stream_pos == folder.stream_sizes[current]:
                        record = stream_map.get(current)
                        if record is not None:
                            if out_file is None:
                                active = dest / record.path
                                active.parent.mkdir(parents=True, exist_ok=True)
                                out_file = open(active, "wb")
                                running_crc = 0
                            out_file.close()
                            out_file = None
                            if record.digest is not None and running_crc != record.digest:
                                active.unlink(missing_ok=True)
                                active = None
                                raise SevenZipError(f"CRC mismatch for {record.path}")
                            active = None
                            add_count(1)
                        current += 1
                        stream_pos = 0
                report()
            current, stream_pos = finish_zero_streams(
                folder, stream_map, current, last_stream, dest
            )
            if current <= last_stream:
                raise SevenZipError("folder ended before all substreams")
        except BaseException:
            if out_file is not None:
                try:
                    out_file.close()
                except OSError:
                    pass
            if active is not None:
                try:
                    active.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    if workers > 1 and open_reader is not None and len(plan) > 1:
        # Largest block first; each worker takes the next block as soon as it is free.
        pending = sorted(plan, key=lambda index: plan[index][2])
        pending_lock = threading.Lock()
        pool_size = min(workers, len(pending))

        local = threading.local()
        readers: list[BinaryIO] = []
        readers_lock = threading.Lock()
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def next_folder() -> int | None:
            with pending_lock:
                return pending.pop() if pending else None

        def worker() -> None:
            try:
                while True:
                    if internal.cancelled or (cancel is not None and cancel.cancelled):
                        raise Cancelled()
                    folder_index = next_folder()
                    if folder_index is None:
                        return
                    reader = getattr(local, "reader", None)
                    if reader is None:
                        reader = open_reader()
                        local.reader = reader
                        with readers_lock:
                            readers.append(reader)
                    extract_folder(reader, folder_index)
            except BaseException as exc:
                with errors_lock:
                    if not isinstance(exc, Cancelled) and not errors:
                        errors.append(exc)
                internal.cancel()
                raise

        executor = ThreadPoolExecutor(max_workers=pool_size)
        try:
            futures = [executor.submit(worker) for _ in range(pool_size)]
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    pass
        finally:
            executor.shutdown(wait=True)
            with readers_lock:
                opened = list(readers)
            for reader in opened:
                try:
                    reader.close()
                except OSError:
                    pass
        if errors:
            raise errors[0]
        if cancel is not None and cancel.cancelled:
            raise Cancelled()
        report()
        return count

    for folder_index in sorted(plan):
        extract_folder(fh, folder_index)
    return count


def extract(
    fh: BinaryIO,
    dest: Path | str,
    *,
    only: Callable[[str], bool] | None = None,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
    workers: int = 1,
    open_reader: Callable[[], BinaryIO] | None = None,
) -> int:
    """Extract every member (or those for which ``only(path)`` is true) into ``dest``.

    Returns the number of regular files written. ``progress(done, total)`` is
    called with unpacked byte counts. A bad path, symlink, unsupported coder or
    CRC mismatch raises :class:`SevenZipError`; the file being written when it
    happens is removed. Cancellation (``cancel``) is checked between chunks.

    With ``workers > 1`` and ``open_reader`` (a zero-argument factory returning
    a new seekable file for the same archive), independent solid blocks are
    decoded in parallel, one reader per worker. Every reader opened here is
    closed before returning.
    """
    try:
        return _extract_archive(
            fh, Path(dest), only, cancel, progress, workers, open_reader
        )
    except Cancelled:
        raise
    except SevenZipError:
        raise
    except (IndexError, struct.error, ValueError, EOFError, lzma.LZMAError, UnicodeDecodeError) as exc:
        raise SevenZipError(str(exc)) from exc
