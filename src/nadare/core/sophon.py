# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""HoYo's "Sophon" chunked downloads.

A Sophon build is a small zstd-compressed protobuf manifest that lists every
file of the game together with the chunks it is made of. Each chunk is its own
zstd frame on the CDN and is addressed by name, so an install can fetch only
the chunks that are missing or damaged and resume at chunk granularity.

Only the standard library plus :mod:`zstandard` is used here; this module
deliberately does not import :mod:`nadare.core.directdl`.
"""

from __future__ import annotations

import hashlib
import http.client
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

from . import download as download_module
from .download import DownloadError
from .events import Cancel, Cancelled, Emitter, Event, emitter

#: Manifest file entries with this ``type`` are directories, not files.
_DIRECTORY_TYPE = 64
#: Never run more chunk workers than this, however large ``workers`` is.
_MAX_WORKERS = 32
#: Marker directory (same as :mod:`directdl`).
STATE_DIR = ".nadare"


class SophonError(RuntimeError):
    """A manifest, a chunk or the local install is invalid."""


@dataclass(frozen=True)
class ChunkEntry:
    name: str
    md5: str
    offset: int
    size: int
    csize: int
    cmd5: str


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    md5: str
    chunks: tuple[ChunkEntry, ...]


@dataclass(frozen=True)
class SophonPackage:
    name: str
    manifest_url: str
    chunk_url: str
    manifest_id: str
    size: int


class _CombinedCancel:
    """Cancellation from either the caller or the sync-local stop token."""

    def __init__(self, user: Cancel | None, local: Cancel) -> None:
        self._user = user
        self._local = local

    @property
    def cancelled(self) -> bool:
        return self._local.cancelled or (self._user is not None and self._user.cancelled)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled("sync cancelled")


# ---------------------------------------------------------------------------
# A tiny protobuf reader
# ---------------------------------------------------------------------------
def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise SophonError("truncated protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise SophonError("protobuf varint is too long")


def _read_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode one message into ``(field_number, wire_type, value)`` triples."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 0x07
        if number == 0:
            raise SophonError("protobuf field number 0")
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data):
                raise SophonError("truncated protobuf fixed64")
            value = data[pos : pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise SophonError("truncated protobuf field")
            value = data[pos : pos + length]
            pos += length
        elif wire == 5:
            if pos + 4 > len(data):
                raise SophonError("truncated protobuf fixed32")
            value = data[pos : pos + 4]
            pos += 4
        else:
            raise SophonError(f"unsupported protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _string(value: bytes, label: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SophonError(f"invalid UTF-8 in {label}") from exc


def _safe_relative_path(path: str) -> str:
    """Return ``path`` or raise :class:`SophonError` for an unsafe one."""
    if not isinstance(path, str) or not path:
        raise SophonError("path must not be empty")
    if "\\" in path:
        raise SophonError(f"path must not contain a backslash: {path!r}")
    if path.startswith("/"):
        raise SophonError(f"path must be relative: {path!r}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise SophonError(f"unsafe path component in {path!r}")
    return path


def _parse_chunk(data: bytes) -> ChunkEntry:
    name = ""
    md5 = ""
    cmd5 = ""
    offset = size = csize = 0
    for number, wire, value in _read_fields(data):
        if number == 1 and wire == 2:
            name = _string(value, "chunk name")
        elif number == 2 and wire == 2:
            md5 = _string(value, "chunk md5")
        elif number == 3 and wire == 0:
            offset = value
        elif number == 4 and wire == 0:
            csize = value
        elif number == 5 and wire == 0:
            size = value
        elif number == 7 and wire == 2:
            cmd5 = _string(value, "chunk compressed md5")
    if not name:
        raise SophonError("chunk has no name")
    return ChunkEntry(name=name, md5=md5, offset=offset, size=size, csize=csize, cmd5=cmd5)


def _parse_file(data: bytes) -> FileEntry | None:
    path = ""
    md5 = ""
    size = 0
    file_type = 0
    chunks: list[ChunkEntry] = []
    for number, wire, value in _read_fields(data):
        if number == 1 and wire == 2:
            path = _string(value, "file name")
        elif number == 2 and wire == 2:
            chunks.append(_parse_chunk(value))
        elif number == 3 and wire == 0:
            file_type = value
        elif number == 4 and wire == 0:
            size = value
        elif number == 5 and wire == 2:
            md5 = _string(value, "file md5")
    if file_type == _DIRECTORY_TYPE:
        return None
    if not path:
        raise SophonError("file has no name")
    path = _safe_relative_path(path)
    return FileEntry(path=path, size=size, md5=md5, chunks=tuple(chunks))


def parse_manifest(data: bytes) -> list[FileEntry]:
    """Parse a decompressed ``Manifest`` protobuf into its files (directories dropped)."""
    entries: list[FileEntry] = []
    for number, wire, value in _read_fields(data):
        if number == 1 and wire == 2:
            entry = _parse_file(value)
            if entry is not None:
                entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# syncing
# ---------------------------------------------------------------------------
def _marker_path(pkg: SophonPackage, root: Path) -> Path:
    return root / STATE_DIR / f"sophon-{pkg.name}.done"


def is_done(pkg: SophonPackage, root: Path) -> bool:
    marker = _marker_path(pkg, root)
    try:
        return marker.read_text(encoding="utf-8") == pkg.manifest_id
    except OSError:
        return False


#: Attempts per request before an error fails the install (the CDN drops a connection now and then).
_ATTEMPTS = 4
#: Largest manifest body accepted (compressed); a real one is under ~30 MB.
_MAX_MANIFEST_BYTES = 256 << 20
#: Fewest seconds between two install progress events (the last one always goes out).
_PROGRESS_INTERVAL = 0.1
#: Chunks of one file handled by one task (a 1 MB-chunk file of 10 GB is spread over many workers).
_CHUNKS_PER_TASK = 32


def _fetch_bytes(
    pool: download_module.HttpClientPool, url: str, limit: int, cancel: Any = None
) -> bytes:
    last: BaseException | None = None
    for attempt in range(_ATTEMPTS):
        if cancel is not None:
            cancel.raise_if_cancelled()
        if attempt:
            time.sleep(min(0.5 * 2**attempt, 8.0))
        try:
            with pool.open(url) as resp:
                if resp.status != 200:
                    raise SophonError(f"HTTP {resp.status} for {url}")
                body = resp.read(limit + 1)
            if len(body) > limit:
                raise SophonError(f"response too large for {url}")
            return body
        except (DownloadError, OSError, http.client.HTTPException) as exc:
            last = exc
        except SophonError as exc:
            last = exc
    raise SophonError(f"cannot download {url}: {last}") from last


def _load_manifest(pool: download_module.HttpClientPool, pkg: SophonPackage) -> list[FileEntry]:
    body = _fetch_bytes(pool, pkg.manifest_url, _MAX_MANIFEST_BYTES)
    try:
        raw = zstandard.ZstdDecompressor().decompress(body, max_output_size=1 << 31)
    except zstandard.ZstdError as exc:
        raise SophonError(f"manifest for {pkg.name} is not a zstd frame: {exc}") from exc
    return parse_manifest(raw)


def _chunk_matches(fh: Any, chunk: ChunkEntry) -> bool:
    """True when the local bytes at ``chunk``'s range have ``chunk.md5``."""
    fh.seek(chunk.offset)
    local = fh.read(chunk.size)
    if len(local) != chunk.size:
        return False
    return hashlib.md5(local).hexdigest().lower() == chunk.md5.lower()


def _fetch_chunk(
    pool: download_module.HttpClientPool, pkg: SophonPackage, chunk: ChunkEntry, cancel: Any
) -> bytes:
    body = _fetch_bytes(
        pool, f"{pkg.chunk_url}/{chunk.name}", max(chunk.csize, chunk.size) + (1 << 16), cancel
    )
    if chunk.cmd5 and hashlib.md5(body).hexdigest().lower() != chunk.cmd5.lower():
        raise SophonError(f"compressed md5 mismatch for chunk {chunk.name!r}")
    try:
        data = zstandard.ZstdDecompressor().decompress(body, max_output_size=chunk.size)
    except zstandard.ZstdError as exc:
        raise SophonError(f"cannot decompress chunk {chunk.name!r}: {exc}") from exc
    if len(data) != chunk.size:
        raise SophonError(
            f"chunk {chunk.name!r} decompressed to {len(data)} bytes, expected {chunk.size}"
        )
    if hashlib.md5(data).hexdigest().lower() != chunk.md5.lower():
        raise SophonError(f"md5 mismatch for chunk {chunk.name!r}")
    return data


def sync_package(
    pkg: SophonPackage,
    root: Path,
    *,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    workers: int = 4,
) -> None:
    """Install ``pkg`` into ``root``, fetching only missing or damaged chunks."""
    say = emitter(emit)
    if cancel is not None:
        cancel.raise_if_cancelled()

    pool = download_module.HttpClientPool()
    try:
        files = _load_manifest(pool, pkg)
        root.mkdir(parents=True, exist_ok=True)
        total = sum(entry.size for entry in files)
        done = 0
        last_emit = -1
        last_time = 0.0
        lock = threading.Lock()

        def add_progress(amount: int) -> None:
            nonlocal done, last_emit, last_time
            with lock:
                done += amount
                now = time.monotonic()
                if done < total and now - last_time < _PROGRESS_INTERVAL:
                    return
                last_time = now
                current = done
                if current == last_emit:
                    return
                last_emit = current
            download_module._emit_progress(say, "install", current, total)

        # Every file exists at its final size before the chunk tasks write into it.
        created: set[str] = set()
        for entry in files:
            target = root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                created.add(entry.path)
            with open(target, "r+b" if target.exists() else "w+b") as fh:
                fh.truncate(entry.size)

        def install_chunks(
            entry: FileEntry, chunks: tuple[ChunkEntry, ...], combined: _CombinedCancel
        ) -> None:
            with open(root / entry.path, "r+b") as fh:
                for chunk in chunks:
                    combined.raise_if_cancelled()
                    # A file created just now is zeros: skip reading it back to hash it.
                    if entry.path in created or not _chunk_matches(fh, chunk):
                        data = _fetch_chunk(pool, pkg, chunk, combined)
                        combined.raise_if_cancelled()
                        fh.seek(chunk.offset)
                        fh.write(data)
                    add_progress(chunk.size)

        tasks = [
            (entry, entry.chunks[i : i + _CHUNKS_PER_TASK])
            for entry in files
            for i in range(0, len(entry.chunks), _CHUNKS_PER_TASK)
        ]
        stop = Cancel()
        combined = _CombinedCancel(cancel, stop)
        errors: list[BaseException] = []
        limit = max(1, min(workers, _MAX_WORKERS, len(tasks) or 1))
        with ThreadPoolExecutor(max_workers=limit) as executor:
            futures = [executor.submit(install_chunks, e, c, combined) for e, c in tasks]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)
                    stop.cancel()
                    for other in futures:
                        other.cancel()
        for exc in errors:
            if not isinstance(exc, Cancelled):
                raise exc
        if errors:
            raise errors[0]
    finally:
        pool.close()

    if total:
        download_module._emit_progress(say, "install", total, total)

    marker = _marker_path(pkg, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(pkg.manifest_id, encoding="utf-8")
    say(Event("log", f"installed {pkg.name}", data={"sophon": pkg.name}))
