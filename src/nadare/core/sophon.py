# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""HoYo's "Sophon" chunked downloads.

A Sophon build is a small zstd-compressed protobuf manifest that lists every
file of the game together with the chunks it is made of. Each chunk is its own
zstd frame on the CDN and is addressed by name, so an install can fetch only
the chunks that are missing or damaged and resume at chunk granularity.

Only the standard library plus :mod:`zstandard` (imported lazily) is used here; this module
deliberately does not import :mod:`nadare.core.directdl`.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diskspace
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


def _zstd() -> Any:
    """The ``zstandard`` module, imported on first use so only Sophon installs need it."""
    try:
        import zstandard
    except ImportError as exc:
        raise SophonError(
            "the 'zstandard' Python package is missing; install it (pip install zstandard)"
        ) from exc
    return zstandard


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


def _target_path(root: Path, relative: str) -> Path:
    """Resolve a manifest path while rejecting symlinks below the install root.

    The root itself may be a symlink; only components beneath it are forbidden.
    """
    relative = _safe_relative_path(relative)
    root_real = Path(os.path.realpath(root))
    target = root / relative
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SophonError(f"cannot inspect install path {relative!r}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise SophonError(f"symlink in install path {relative!r}")

    try:
        parent_real = Path(os.path.realpath(target.parent))
        if os.path.commonpath((str(root_real), str(parent_real))) != str(root_real):
            raise SophonError(f"install path escapes root: {relative!r}")
    except ValueError as exc:
        raise SophonError(f"install path escapes root: {relative!r}") from exc
    return target


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


def _ledger_path(pkg: SophonPackage, root: Path) -> Path:
    return root / STATE_DIR / f"sophon-{pkg.name}.files"


def _validate_ledger_entry(entry: str, ledger: Path) -> str:
    """Return a ledger path, or raise :class:`SophonError` when it is unsafe."""
    if not isinstance(entry, str) or not entry:
        raise SophonError(f"unsafe empty path in {ledger.name}")
    if "\\" in entry:
        raise SophonError(f"unsafe backslash path in {ledger.name}: {entry!r}")
    if entry.startswith("/"):
        raise SophonError(f"unsafe absolute path in {ledger.name}: {entry!r}")
    if any(part in ("", ".", "..") for part in entry.split("/")):
        raise SophonError(f"unsafe path in {ledger.name}: {entry!r}")
    return entry


def _ensure_safe_parents(root: Path, relative: str) -> None:
    """Reject a ledger path reached through a symlinked parent directory."""
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SophonError(f"unsafe path {relative!r}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SophonError(
                f"unsafe path {relative!r}: parent {part!r} is a symlink"
            )
        if not stat.S_ISDIR(info.st_mode):
            return


def _check_state_storage(root: Path, path: Path | None = None) -> None:
    """Refuse metadata paths whose .nadare directory or target is a symlink."""
    state = root / STATE_DIR
    try:
        info = state.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SophonError(f"cannot inspect {STATE_DIR}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise SophonError(f"{STATE_DIR} metadata directory is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise SophonError(f"{STATE_DIR} metadata path is not a directory")
    if path is not None:
        try:
            target_info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SophonError(f"cannot inspect {path.name}: {exc}") from exc
        if stat.S_ISLNK(target_info.st_mode):
            raise SophonError(f"{path.name} metadata file is a symlink")
        if not stat.S_ISREG(target_info.st_mode):
            raise SophonError(f"{path.name} metadata path is not a regular file")


def _read_ledger(pkg: SophonPackage, root: Path) -> tuple[list[str], str | None]:
    """Read prior paths and raw text, validating metadata before anything is written."""
    ledger = _ledger_path(pkg, root)
    _check_state_storage(root, ledger)
    try:
        raw = ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        raise SophonError(f"cannot read {ledger.name}: {exc}") from exc
    except UnicodeError as exc:
        raise SophonError(f"invalid text in {ledger.name}: {exc}") from exc
    entries: list[str] = []
    for line in raw.splitlines():
        if not line:
            continue
        entries.append(_validate_ledger_entry(line, ledger))
    return entries, raw


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` through a temporary file and ``os.replace`` (never the target)."""
    if path.parent.name == STATE_DIR:
        _check_state_storage(path.parent.parent, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _other_package_paths(root: Path, current_ledger: Path) -> set[str] | None:
    """Read sibling package ledgers; return None when ownership is uncertain."""
    state = root / STATE_DIR
    try:
        mode = state.lstat().st_mode
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        return None

    protected: set[str] = set()
    try:
        ledgers = state.glob("sophon-*.files")
        for ledger in ledgers:
            if ledger == current_ledger:
                continue
            _check_state_storage(root, ledger)
            raw = ledger.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if line:
                    protected.add(_validate_ledger_entry(line, ledger))
    except (OSError, UnicodeError, SophonError):
        # If another package's ownership data is unreadable, keep every leftover.
        return None
    return protected


def _cleanup_stale_files(
    root: Path, previous: list[str], current: list[str], current_ledger: Path
) -> None:
    """Remove unowned stale ledger paths, then prune empty directories."""
    protected = _other_package_paths(root, current_ledger)
    if protected is None:
        return
    keep = set(current) | protected
    parents: set[Path] = set()
    for relative in previous:
        if relative in keep or relative.split("/", 1)[0] == STATE_DIR:
            continue
        try:
            # Re-check at deletion time: a parent may have become a symlink meanwhile.
            _ensure_safe_parents(root, relative)
        except SophonError:
            continue
        try:
            (root / relative).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best effort: one stubborn file must not fail an otherwise good install.
            continue
        parents.add(Path(relative).parent)
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        if not parent.parts or parent == Path("."):
            continue
        target = root / parent
        while target != root:
            try:
                _ensure_safe_parents(root, target.relative_to(root).as_posix() + "/.nadare-prune")
                info = target.lstat()
            except (OSError, SophonError):
                break
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                break
            try:
                target.rmdir()  # only succeeds while the directory is empty
            except OSError:
                break
            target = target.parent


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
        raw = _zstd().ZstdDecompressor().decompress(body, max_output_size=1 << 31)
    except _zstd().ZstdError as exc:
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
        data = _zstd().ZstdDecompressor().decompress(body, max_output_size=chunk.size)
    except _zstd().ZstdError as exc:
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
    """Install ``pkg`` into ``root``, fetching only missing or damaged chunks.

    Every sync reloads the manifest and verifies the local chunks even when the
    ``.done`` marker matches; re-sync is also the repair path for damaged files.
    """
    say = emitter(emit)
    if cancel is not None:
        cancel.raise_if_cancelled()

    pool = download_module.HttpClientPool()
    staging_paths: list[Path] = []
    try:
        files = _load_manifest(pool, pkg)
        previous, previous_text = _read_ledger(pkg, root)
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

        matched_chunks: dict[str, set[ChunkEntry]] = {}
        existing: set[str] = set()
        for entry in files:
            target = _target_path(root, entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _target_path(root, entry.path)
            temporary = _target_path(root, entry.path + ".nadare-new")
            # Stale staging files are owned by this sync path; clean them lazily
            # instead of walking the full install tree on every update.
            if temporary.is_file() or temporary.is_symlink():
                temporary.unlink()
            if target.is_file():
                existing.add(entry.path)

        def inspect_file(entry: FileEntry) -> set[ChunkEntry]:
            target = _target_path(root, entry.path)
            with open(target, "rb") as old_file:
                return {chunk for chunk in entry.chunks if _chunk_matches(old_file, chunk)}

        # Check installed files concurrently. The matching chunk set is reused
        # below, so changed files do not hash the old bytes a second time.
        inspect_entries = [entry for entry in files if entry.path in existing]
        if inspect_entries:
            inspect_workers = max(1, min(workers, _MAX_WORKERS, len(inspect_entries)))
            with ThreadPoolExecutor(max_workers=inspect_workers) as executor:
                futures = {
                    executor.submit(inspect_file, entry): entry for entry in inspect_entries
                }
                for future in as_completed(futures):
                    entry = futures[future]
                    matched_chunks[entry.path] = future.result()

        unchanged: set[str] = set()
        for entry in files:
            if (
                entry.path in existing
                and _target_path(root, entry.path).stat().st_size == entry.size
                and len(matched_chunks[entry.path]) == len(entry.chunks)
            ):
                unchanged.add(entry.path)
                for chunk in entry.chunks:
                    add_progress(chunk.size)

        def install_chunks(
            entry: FileEntry,
            chunks: tuple[ChunkEntry, ...],
            output_path: Path,
            old_path: Path | None,
            matched: set[ChunkEntry],
            combined: _CombinedCancel,
        ) -> None:
            output_relative = entry.path + ".nadare-new" if old_path is not None else entry.path
            output_path = _target_path(root, output_relative)
            if old_path is not None:
                old_path = _target_path(root, entry.path)
            with open(output_path, "r+b") as output:
                old_file = open(old_path, "rb") if old_path is not None else None
                try:
                    for chunk in chunks:
                        combined.raise_if_cancelled()
                        if old_file is not None and chunk in matched:
                            old_file.seek(chunk.offset)
                            data = old_file.read(chunk.size)
                        else:
                            data = _fetch_chunk(pool, pkg, chunk, combined)
                            combined.raise_if_cancelled()
                        output.seek(chunk.offset)
                        output.write(data)
                        add_progress(chunk.size)
                finally:
                    if old_file is not None:
                        old_file.close()

        def run_tasks(
            task_items: list[
                tuple[FileEntry, tuple[ChunkEntry, ...], Path, Path | None, set[ChunkEntry]]
            ],
        ) -> None:
            if not task_items:
                return
            stop = Cancel()
            combined = _CombinedCancel(cancel, stop)
            errors: list[BaseException] = []
            limit = max(1, min(workers, _MAX_WORKERS, len(task_items)))
            with ThreadPoolExecutor(max_workers=limit) as executor:
                futures = [
                    executor.submit(install_chunks, entry, chunks, output, old, matched, combined)
                    for entry, chunks, output, old, matched in task_items
                ]
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

        def batches(entry: FileEntry) -> list[tuple[ChunkEntry, ...]]:
            return [
                entry.chunks[i : i + _CHUNKS_PER_TASK]
                for i in range(0, len(entry.chunks), _CHUNKS_PER_TASK)
            ]

        # New files can be written in place: there is no old copy to protect,
        # and keeping the final path lets an interrupted first install resume.
        fresh_entries = [entry for entry in files if entry.path not in existing]
        fresh_tasks: list[
            tuple[FileEntry, tuple[ChunkEntry, ...], Path, Path | None, set[ChunkEntry]]
        ] = []
        for entry in fresh_entries:
            target = _target_path(root, entry.path)
            with open(target, "w+b") as fh:
                fh.truncate(entry.size)
            fresh_tasks.extend(
                (entry, chunk_batch, target, None, set()) for chunk_batch in batches(entry)
            )
        run_tasks(fresh_tasks)

        # Stage one changed existing file at a time. Each file is swapped in as
        # soon as its chunk batches finish, so staging needs at most that file's
        # size in additional disk space.
        for entry in files:
            if entry.path not in existing or entry.path in unchanged:
                continue
            target = _target_path(root, entry.path)
            diskspace.check_space(target.parent, entry.size)
            temporary = _target_path(root, entry.path + ".nadare-new")
            staging_paths.append(temporary)
            with open(temporary, "w+b") as fh:
                fh.truncate(entry.size)
            update_tasks = [
                (entry, chunk_batch, temporary, target, matched_chunks[entry.path])
                for chunk_batch in batches(entry)
            ]
            run_tasks(update_tasks)
            target = _target_path(root, entry.path)
            temporary = _target_path(root, entry.path + ".nadare-new")
            shutil.copymode(target, temporary)
            with open(temporary, "r+b") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, target)
    finally:
        pool.close()
        for temporary in staging_paths:
            try:
                relative = temporary.relative_to(root).as_posix()
                _target_path(root, relative).unlink(missing_ok=True)
            except (OSError, SophonError):
                pass

    if total:
        download_module._emit_progress(say, "install", total, total)

    current = list(dict.fromkeys(entry.path for entry in files))
    ledger = _ledger_path(pkg, root)
    marker = _marker_path(pkg, root)
    # Publish the new ownership set before best-effort cleanup. If pruning fails
    # partway, the old paths are no longer in the ledger and will not be retried.
    _atomic_write_text(ledger, "".join(f"{path}\n" for path in current))
    try:
        _atomic_write_text(marker, pkg.manifest_id)
    except BaseException:
        try:
            if previous_text is None:
                ledger.unlink(missing_ok=True)
            else:
                _atomic_write_text(ledger, previous_text)
        except OSError:
            pass
        raise
    _cleanup_stale_files(root, previous, current, ledger)
    say(Event("log", f"installed {pkg.name}", data={"sophon": pkg.name}))
