# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct game downloads from a publisher's public patch API.

Where :mod:`nadare.core.yostar` consumes a CI-signed Yostar manifest, this module
installs games whose publisher exposes a plain, unauthenticated version/manifest
endpoint. A per-game *source* (``nadare.core.sources.<game>``) turns that
endpoint into a :class:`Plan`; :func:`sync` installs the plan.

A plan has two parts:

``files``
    individual files, each with a URL and an md5, crc32 or sha256 (or none).
``archives``
    a game shipped as one zip, as a zip containing a zip, or as a zip split into
    ordered parts. They are downloaded into ``<dest>/.nadare-dl``, verified,
    unwrapped into ``dest`` and then deleted.

Nothing is written outside ``dest``; every path is relative and traversal-free.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diskspace, paths, sevenz, sophon
from . import download as download_module
from .download import DownloadError, download, remote_size
from .events import Cancel, Cancelled, Emitter, Event, emitter
from .rangefile import RangeError, RangePart, RangeReader

#: Cache directory (under the install dir) for in-flight archive downloads.
#: Most files fetched in parallel. A game with tens of thousands of small files is latency-bound
#: per connection, so one install needs well over 16 connections to fill a fast line.
MAX_WORKERS = 64
_PLAN_CHECK_MAX_WORKERS = 16
DL_DIR = ".nadare-dl"
#: Directory (under the install dir) holding one ``<name>.done`` per unwrapped archive.
STATE_DIR = ".nadare"

#: Block size for the Range reader used by streamed zip installs. Smaller than the
#: reader's default so an update only fetches the blocks around changed members.
_STREAM_BLOCK = 64 << 10
#: Sequential member reads fetch 4 MB per request; random access still fetches one
#: 64 KB block.
_STREAM_READAHEAD = 4 << 20
#: Most solid blocks of a streamed 7z decoded at once. Each worker holds a decoder (tens of MB of
#: LZMA dictionary) and a connection; 8 is enough to fill a fast line on one machine.
_MAX_7Z_WORKERS = 8
#: Size of the chunks copied out of a zip member (and counted for progress).
_STREAM_CHUNK = 1 << 20


class DirectError(RuntimeError):
    """The plan is invalid, a download failed, or a hash/size did not match."""


@dataclass(frozen=True)
class RemoteFile:
    """One downloadable object.

    ``path`` is relative to the install dir (for a plain file) or a cache file
    name (for an archive part). ``md5``/``sha256`` are lowercase-insensitive hex,
    ``crc32`` is 8 hex digits. ``headers`` are sent with the request (e.g. Referer).
    """

    path: str
    url: str
    size: int | None = None
    md5: str | None = None
    sha256: str | None = None
    crc32: str | None = None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Member:
    """A file stored as a byte range inside a downloaded pack (``unwrap=("slices",)``)."""

    path: str
    offset: int
    size: int
    md5: str | None = None
    crc32: str | None = None


@dataclass(frozen=True)
class Archive:
    """A game shipped as an archive.

    ``source`` is the downloaded object (or, when ``parts`` is non-empty, the
    *concatenation* of ``parts`` in order, verified against ``source``'s hashes).
    ``unwrap`` is ``("zip",)``, ``("7z",)``, ``("slices",)`` or ``("zip", "zip")``. With
    ``("zip", "zip")`` the outer zip must contain exactly one file, which is itself
    a zip extracted into ``dest``; ``inner_crc32`` (if set) is checked against that
    one inner file before the second extraction.
    """

    name: str
    source: RemoteFile
    parts: tuple[RemoteFile, ...] = ()
    unwrap: tuple[str, ...] = ("zip",)
    inner_crc32: str | None = None
    #: With ``unwrap == ("slices",)``: the files to cut out of the downloaded pack
    #: (``source``, or the joined ``parts``), each ``pack[offset : offset + size]``.
    members: tuple[Member, ...] = ()
    #: With ``unwrap == ("zip",)``: read the zip straight off the server by Range
    #: requests and write each member where it belongs, instead of downloading the
    #: archive first. Members are checked by their own CRC-32 (the declared archive
    #: hashes are not read), files that already match are skipped, so an update or a
    #: resumed install only fetches what differs. Ignored for other kinds.
    stream: bool = False


@dataclass(frozen=True)
class Plan:
    game_tag: str
    version: str
    files: tuple[RemoteFile, ...] = ()
    archives: tuple[Archive, ...] = ()
    #: Relative path of the game executable the install should produce.
    exe: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: HoYo-style chunked packages, installed after the files and archives.
    sophon: tuple[sophon.SophonPackage, ...] = ()


class _CombinedCancel:
    """A cancellation token set by either the caller or a sync-local stop."""

    def __init__(self, user: Cancel | None, local: Cancel) -> None:
        self._user = user
        self._local = local

    @property
    def cancelled(self) -> bool:
        return self._local.cancelled or (self._user is not None and self._user.cancelled)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled("sync cancelled")


def _remove_file(path: Path) -> None:
    for candidate in (path, Path(f"{path}.part")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


class _JoinedFile(io.RawIOBase):
    """A read-only, seekable file over ordered parts, without joining them on disk.

    At most one part is open at a time; reads crossing a boundary are stitched
    together by :meth:`readinto`.
    """

    def __init__(self, paths: list[Path] | tuple[Path, ...]) -> None:
        super().__init__()
        self._paths = list(paths)
        self._sizes = [path.stat().st_size for path in self._paths]
        self._offsets: list[int] = []
        total = 0
        for size in self._sizes:
            self._offsets.append(total)
            total += size
        self.size = total
        self.name = self._paths[0].name if self._paths else "joined"
        self._pos = 0
        self._index = -1
        self._fh: io.BufferedReader | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def _close_part(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._index = -1

    def _open_at(self, pos: int) -> io.BufferedReader | None:
        index = -1
        for i, offset in enumerate(self._offsets):
            if offset <= pos < offset + self._sizes[i]:
                index = i
                break
        if index < 0:
            return None
        if index != self._index:
            self._close_part()
            self._fh = open(self._paths[index], "rb")
            self._index = index
        assert self._fh is not None
        self._fh.seek(pos - self._offsets[index])
        return self._fh

    def readinto(self, buffer: Any) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        view = memoryview(buffer).cast("B")
        wanted = len(view)
        written = 0
        while written < wanted and self._pos < self.size:
            fh = self._open_at(self._pos)
            if fh is None:
                break
            chunk = fh.read(wanted - written)
            if not chunk:
                break
            view[written : written + len(chunk)] = chunk
            written += len(chunk)
            self._pos += len(chunk)
        return written

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self.size + offset
        else:
            raise ValueError(f"invalid whence: {whence!r}")
        if target < 0:
            raise ValueError(f"negative seek position: {target}")
        self._pos = target
        return self._pos

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self._close_part()
        super().close()


def _hash_parts(paths: list[Path], rf: RemoteFile, name: str) -> None:
    """Verify the concatenation of ``paths`` against ``rf`` without joining it."""
    size = sum(path.stat().st_size for path in paths)
    algorithms = tuple(alg for alg in ("md5", "sha256") if getattr(rf, alg))
    hashers = {alg: hashlib.new(alg) for alg in algorithms}
    crc = 0
    for path in paths:
        with open(path, "rb") as fh:
            while block := fh.read(1 << 20):
                for hasher in hashers.values():
                    hasher.update(block)
                crc = zlib.crc32(block, crc)
    mismatch = rf.size is not None and size != rf.size
    if not mismatch:
        for alg, hasher in hashers.items():
            if hasher.hexdigest().lower() != str(getattr(rf, alg)).lower():
                mismatch = True
                break
    if not mismatch and rf.crc32 and f"{crc & 0xFFFFFFFF:08X}" != rf.crc32.upper():
        mismatch = True
    if mismatch:
        raise DirectError(f"archive hash/size mismatch for {name}")


def _crc32_file(path: Path) -> str:
    value = 0
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            value = zlib.crc32(block, value)
    return f"{value & 0xFFFFFFFF:08X}"


def _digest_pass(
    path: Path, algorithms: tuple[str, ...], want_crc: bool, cancel: Cancel | None
) -> tuple[dict[str, str], str]:
    """One read pass over ``path`` for every wanted digest, checking ``cancel`` per block.

    A resumed install re-hashes every file on disk (tens of GB on a NAS), so Cancel has to be
    able to stop it between blocks instead of waiting for the scan to end (#562).
    """
    hashers = {name: hashlib.new(name) for name in algorithms}
    crc = 0
    with open(path, "rb") as fh:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            block = fh.read(1 << 20)
            if not block:
                break
            for hasher in hashers.values():
                hasher.update(block)
            if want_crc:
                crc = zlib.crc32(block, crc)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}, f"{crc & 0xFFFFFFFF:08X}"


def _verify_remote(
    path: Path, rf: RemoteFile, *, hashes: bool = True, cancel: Cancel | None = None
) -> bool:
    try:
        if cancel is not None:
            cancel.raise_if_cancelled()
        if not path.is_file():
            return False
        if rf.size is not None and path.stat().st_size != rf.size:
            return False
        if not hashes:
            return True
        algorithms = tuple(name for name in ("md5", "sha256") if getattr(rf, name))
        if not algorithms and not rf.crc32:
            return True
        digests, crc = _digest_pass(path, algorithms, bool(rf.crc32), cancel)
        for name in algorithms:
            if digests[name].lower() != getattr(rf, name).lower():
                return False
        if rf.crc32 and crc != rf.crc32.upper():
            return False
    except OSError:
        return False
    return True


def _download_remote(
    rf: RemoteFile,
    path: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()
    size = rf.size
    if size is None:
        # Some APIs report 0 for a multi-GB archive; ask the server instead of
        # falling back to the downloader's cap for undeclared sizes.
        size = remote_size(rf.url, headers=dict(rf.headers))
    if size:
        diskspace.check_space(path.parent, size)
    preserve_existing = path.exists()
    if (
        preserve_existing
        and (rf.md5 or rf.sha256 or rf.crc32)
        and _verify_remote(path, rf, cancel=cancel)
    ):
        # Already here and verified against a declared hash (e.g. the archive kept after a
        # failed unpack): downloading it again would only double the bytes.
        return
    temporary = path.with_name(path.name + ".nadare-new")
    destination = temporary if preserve_existing else path
    try:
        download(
            rf.url,
            destination,
            expected_size=size,
            expected_sha256=rf.sha256,
            headers=dict(rf.headers),
            emit=emit,
            cancel=cancel,
            resume=True,
        )
        if (rf.md5 or rf.crc32) and not _verify_remote(destination, rf):
            raise DirectError(f"hash mismatch for {rf.path}")
        if preserve_existing:
            os.replace(temporary, path)
    except DirectError:
        # A corrupt staged copy must never remove the installed file.
        _remove_file(destination)
        _remove_file(destination.with_name(destination.name + ".part"))
        raise
    except DownloadError as exc:
        if preserve_existing:
            _remove_file(temporary)
            _remove_file(temporary.with_name(temporary.name + ".part"))
        raise DirectError(f"download failed for {rf.path}: {exc}") from exc
    except BaseException:
        # Cancellation or a local write error keeps the .part for resume.
        raise


#: Most member names (with size and CRC-32) one ``.done`` record keeps.
_MAX_RECORDED_MEMBERS = 100_000


def _done_record(
    plan: Plan, archive: Archive, members: Sequence[str] | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {"version": plan.version, "url": archive.source.url}
    for name in ("md5", "sha256", "crc32"):
        value = getattr(archive.source, name)
        if value:
            record[name] = value.lower()
    if members is not None:
        sorted_members = sorted(set(members))
        recorded_members = sorted_members[:_MAX_RECORDED_MEMBERS]
        record["members"] = recorded_members
        if len(sorted_members) > _MAX_RECORDED_MEMBERS:
            record["members_truncated"] = True
    return record


def _archive_done(plan: Plan, archive: Archive, dest: Path, *, hashes: bool = False) -> bool:
    name = safe_relative_path(archive.name)
    record_path = dest / STATE_DIR / f"{name}.done"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    if record.get("version") != plan.version or record.get("url") != archive.source.url:
        return False
    for name in ("md5", "sha256", "crc32"):
        expected = getattr(archive.source, name)
        actual = record.get(name)
        if expected is None:
            if actual not in (None, ""):
                return False
        elif not isinstance(actual, str) or actual.lower() != expected.lower():
            return False
    members = record.get("members")
    if members is not None:
        if not isinstance(members, list):
            return False
        member_sizes = record.get("member_sizes")
        if member_sizes is not None and not isinstance(member_sizes, dict):
            return False
        member_crcs = record.get("member_crc32")
        if member_crcs is not None and not isinstance(member_crcs, dict):
            return False
        for member in members:
            if not isinstance(member, str):
                return False
            try:
                path = dest / safe_relative_path(member)
            except DirectError:
                return False
            try:
                if not path.is_file():
                    return False
                if member_sizes is not None:
                    expected_size = member_sizes.get(member)
                    if (
                        isinstance(expected_size, bool)
                        or not isinstance(expected_size, int)
                        or expected_size < 0
                        or path.stat().st_size != expected_size
                    ):
                        return False
                if hashes and member_crcs is not None:
                    expected_crc = member_crcs.get(member)
                    if (
                        not isinstance(expected_crc, str)
                        or _crc32_file(path) != expected_crc.upper()
                    ):
                        return False
            except OSError:
                return False
    return True


def _zip_entries(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    for info in zf.infolist():
        if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
            raise DirectError(f"zip member is a symlink: {info.filename!r}")
        entries.append((info, safe_relative_path(info.filename)))
    return entries


def _extract_zip_to(zip_path: Path | _JoinedFile, target: Path) -> list[str]:
    """Extract ``zip_path`` into ``target`` and return its relative file paths."""
    try:
        written: list[str] = []
        with zipfile.ZipFile(zip_path) as zf:
            entries = _zip_entries(zf)
            target.mkdir(parents=True, exist_ok=True)
            for info, relative in entries:
                out = target / relative
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                written.append(relative)
        return written
    except DirectError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile) as exc:
        raise DirectError(f"cannot extract {zip_path.name}: {exc}") from exc


def _safe_destination_path(dest: Path, relative: str) -> Path:
    """Return a destination path that has no symlinked components below ``dest``."""
    relative = safe_relative_path(relative)
    dest_real = Path(os.path.realpath(dest))
    target = dest / relative
    current = dest
    for part in relative.split("/"):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DirectError(f"cannot inspect install path {relative!r}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise DirectError(f"symlink in install path {relative!r}")
    try:
        parent_real = Path(os.path.realpath(target.parent))
        if os.path.commonpath((str(dest_real), str(parent_real))) != str(dest_real):
            raise DirectError(f"install path escapes root: {relative!r}")
    except ValueError as exc:
        raise DirectError(f"install path escapes root: {relative!r}") from exc
    return target


def _merge_tree(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        target = _safe_destination_path(dest, relative)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _safe_destination_path(dest, relative)
            try:
                os.replace(path, target)
            except OSError as exc:
                raise DirectError(f"cannot move {relative} into the install: {exc}") from exc


def _extract_zip_into_dest(
    zip_path: Path | _JoinedFile, dest: Path, dl_dir: Path
) -> list[str]:
    staging = Path(tempfile.mkdtemp(prefix="extract-", dir=dl_dir))
    try:
        members = _extract_zip_to(zip_path, staging)
        _merge_tree(staging, dest)
        return members
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_slices_into_dest(
    pack: Path | _JoinedFile, members: tuple[Member, ...], dest: Path, dl_dir: Path
) -> list[str]:
    pack_size = pack.size if isinstance(pack, _JoinedFile) else pack.stat().st_size
    for member in members:
        if member.offset + member.size > pack_size:
            raise DirectError(
                f"slice {member.path!r} runs past the end of the pack "
                f"({member.offset + member.size} > {pack_size})"
            )

    staging = Path(tempfile.mkdtemp(prefix="slices-", dir=dl_dir))
    joined = isinstance(pack, _JoinedFile)
    fh = pack if joined else open(pack, "rb")
    try:
        for member in members:
            relative = safe_relative_path(member.path)
            out = staging / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            hashers = {}
            if member.md5:
                hashers["md5"] = hashlib.md5()
            if member.crc32:
                hashers["crc32"] = 0
            fh.seek(member.offset)
            remaining = member.size
            with open(out, "wb") as dst:
                while remaining:
                    block = fh.read(min(1 << 20, remaining))
                    if not block:
                        raise DirectError(
                            f"slice {member.path!r} ended before {member.size} bytes"
                        )
                    dst.write(block)
                    remaining -= len(block)
                    if "md5" in hashers:
                        hashers["md5"].update(block)
                    if "crc32" in hashers:
                        hashers["crc32"] = zlib.crc32(block, hashers["crc32"])
            if "md5" in hashers:
                if hashers["md5"].hexdigest().lower() != member.md5.lower():
                    raise DirectError(f"md5 mismatch for slice {member.path!r}")
            if "crc32" in hashers:
                if f"{hashers['crc32'] & 0xFFFFFFFF:08X}" != member.crc32.upper():
                    raise DirectError(f"crc32 mismatch for slice {member.path!r}")
        _merge_tree(staging, dest)
        return [safe_relative_path(member.path) for member in members]
    finally:
        if not joined:
            fh.close()
        shutil.rmtree(staging, ignore_errors=True)


def _extract_7z_into_dest(
    source: Path | _JoinedFile, dest: Path, dl_dir: Path, cancel: Cancel | None
) -> list[str]:
    staging = Path(tempfile.mkdtemp(prefix="extract-", dir=dl_dir))
    try:
        if isinstance(source, Path):
            with open(source, "rb") as fh:
                entries = sevenz.list_entries(fh)
                fh.seek(0)
                sevenz.extract(fh, staging, cancel=cancel)
        else:
            entries = sevenz.list_entries(source)
            source.seek(0)
            sevenz.extract(source, staging, cancel=cancel)
        _merge_tree(staging, dest)
        return [entry.path for entry in entries if not entry.is_dir]
    except (sevenz.SevenZipError, OSError) as exc:
        raise DirectError(f"cannot extract the 7z archive: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _unwrap(
    archive: Archive,
    source: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None = None,
) -> list[str]:
    if archive.unwrap == ("7z",):
        return _extract_7z_into_dest(source, dest, dl_dir, cancel)
    if archive.unwrap == ("slices",):
        return _extract_slices_into_dest(source, archive.members, dest, dl_dir)
    if archive.unwrap == ("zip",):
        return _extract_zip_into_dest(source, dest, dl_dir)
    if archive.unwrap != ("zip", "zip"):
        raise DirectError(f"unsupported archive unwrap for {archive.name}: {archive.unwrap!r}")

    outer = Path(tempfile.mkdtemp(prefix="outer-", dir=dl_dir))
    try:
        _extract_zip_to(source, outer)
        regular = [path for path in outer.rglob("*") if path.is_file()]
        if len(regular) != 1:
            raise DirectError(
                f"nested archive {archive.name} must contain exactly one file, "
                f"found {len(regular)}"
            )
        inner = regular[0]
        if archive.inner_crc32 and _crc32_file(inner) != archive.inner_crc32.upper():
            raise DirectError(f"inner crc32 mismatch for {archive.name}")
        return _extract_zip_into_dest(inner, dest, dl_dir)
    finally:
        shutil.rmtree(outer, ignore_errors=True)


def _write_done(
    plan: Plan, archive: Archive, dest: Path, members: Sequence[str] | None = None
) -> None:
    name = safe_relative_path(archive.name)
    _check_work_dirs(dest)
    record_path = dest / STATE_DIR / f"{name}.done"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = record_path.with_name(f"{record_path.name}.tmp")
    record = _done_record(plan, archive, members)
    if members is not None:
        record["member_sizes"] = {
            member: (dest / safe_relative_path(member)).stat().st_size
            for member in record["members"]
        }
        record["member_crc32"] = {
            member: _crc32_file(dest / safe_relative_path(member))
            for member in record["members"]
        }
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, record_path)


def _stream_member_current(path: Path, info: zipfile.ZipInfo) -> bool:
    """True when ``path`` is the regular file described by ``info``."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != info.file_size:
            return False
        return int(_crc32_file(path), 16) == (info.CRC & 0xFFFFFFFF)
    except OSError:
        return False


def _stream_reader(
    parts: Sequence[RemoteFile], headers: Mapping[str, str], cancel: Cancel | None
) -> RangeReader:
    ranges = [RangePart(rf.url, rf.size) for rf in parts]
    return RangeReader(
        ranges,
        headers=headers,
        block_size=_STREAM_BLOCK,
        readahead=_STREAM_READAHEAD,
        cancel=cancel,
    )


def _balanced_runs(
    items: list[tuple[zipfile.ZipInfo, str]], limit: int
) -> list[list[tuple[zipfile.ZipInfo, str]]]:
    """Split ``items`` into at most ``limit`` contiguous, size-balanced runs."""
    if not items:
        return []
    if limit <= 1 or len(items) <= 1:
        return [items]

    sizes = [info.compress_size for info, _ in items]
    total = sum(sizes)
    biggest = max(range(len(items)), key=lambda index: sizes[index])
    if total > 0 and sizes[biggest] * 2 >= total:
        # One member dominates: keep it alone and split the two sides around it.
        remaining = limit - 1
        left = items[:biggest]
        right = items[biggest + 1 :]
        if left and right and remaining >= 2:
            left_limit = max(1, remaining // 2)
            right_limit = max(1, remaining - left_limit)
            return (
                _balanced_runs(left, left_limit)
                + [[items[biggest]]]
                + _balanced_runs(right, right_limit)
            )

    target = total / limit if total else len(items) / limit
    runs: list[list[tuple[zipfile.ZipInfo, str]]] = []
    current: list[tuple[zipfile.ZipInfo, str]] = []
    current_size = 0.0
    for item in items:
        if current and current_size >= target and len(runs) < limit - 1:
            runs.append(current)
            current = []
            current_size = 0.0
        current.append(item)
        current_size += item[0].compress_size
    if current:
        runs.append(current)
    return runs


def _check_work_dirs(dest: Path) -> None:
    """Refuse an install whose scratch or state folder is a symlink.

    ``.nadare-dl`` and ``.nadare`` hold the archive cache, staging and ``.done`` records, and a
    cleanup ``rmtree``s inside them: through a link that would write to, and delete from, a
    folder outside the install root.
    """
    for name in (DL_DIR, STATE_DIR):
        try:
            mode = (dest / name).lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DirectError(f"cannot inspect {name}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise DirectError(f"{name} in the install folder is a symlink; remove it and retry")
        if not stat.S_ISDIR(mode):
            raise DirectError(f"{name} in the install folder is not a folder")


#: Prefixes of the per-run staging folders created under ``.nadare-dl``.
_STAGING_PREFIXES = ("extract-", "slices-", "outer-")


def _sweep_stale_staging(root: Path) -> None:
    """Remove staging folders a killed run left in ``.nadare-dl``.

    Only called while the install lock is held, so nothing else is using them.
    """
    dl_dir = root / DL_DIR
    if dl_dir.is_symlink() or not dl_dir.is_dir():
        return
    try:
        entries = list(dl_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(_STAGING_PREFIXES) and not entry.is_symlink() and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _cleanup_stream_dir(dest: Path) -> None:
    if (dest / DL_DIR).is_symlink():
        return  # never delete through a link
    stream_dir = dest / DL_DIR / "stream"
    shutil.rmtree(stream_dir, ignore_errors=True)
    try:
        (dest / DL_DIR).rmdir()
    except OSError:
        pass


def _install_streamed_zip(
    plan: Plan,
    archive: Archive,
    dest: Path,
    zf: zipfile.ZipFile,
    main_reader: RangeReader,
    parts: Sequence[RemoteFile],
    entries: list[tuple[zipfile.ZipInfo, str]],
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
) -> None:
    """Write the members of an already-open remote zip into ``dest``."""
    for info, relative in entries:
        if info.is_dir():
            _safe_destination_path(dest, relative).mkdir(parents=True, exist_ok=True)

    todo: list[tuple[zipfile.ZipInfo, str]] = []
    for info, relative in entries:
        if info.is_dir():
            continue
        if not _stream_member_current(_safe_destination_path(dest, relative), info):
            todo.append((info, relative))

    total = sum(info.file_size for info, _ in todo)
    diskspace.check_space(dest, total)

    say = emitter(emit)
    done = 0
    last_emit = 0
    lock = threading.Lock()

    def add_progress(amount: int) -> None:
        nonlocal done, last_emit
        with lock:
            done += amount
            if total and done - last_emit >= _STREAM_CHUNK:
                last_emit = done
                current = done
            else:
                return
        download_module._emit_progress(say, "install", current, total)

    def fetch_member(
        info: zipfile.ZipInfo,
        relative: str,
        local_zf: zipfile.ZipFile,
        stream_dir: Path,
        combined: _CombinedCancel,
    ) -> None:
        fd, name = tempfile.mkstemp(prefix="member-", suffix=".part", dir=stream_dir)
        os.close(fd)
        temporary = Path(name)
        try:
            with local_zf.open(info) as src, open(temporary, "wb") as dst:
                while True:
                    combined.raise_if_cancelled()
                    block = src.read(_STREAM_CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    add_progress(len(block))
            out = _safe_destination_path(dest, relative)
            out.parent.mkdir(parents=True, exist_ok=True)
            out = _safe_destination_path(dest, relative)
            os.replace(temporary, out)
        except Cancelled:
            _remove_file(temporary)
            raise
        except (zipfile.BadZipFile, zlib.error, OSError, RangeError) as exc:
            _remove_file(temporary)
            raise DirectError(
                f"cannot install zip member {relative!r} from {archive.name}: {exc}"
            ) from exc
        except BaseException:
            _remove_file(temporary)
            raise

    if todo:
        todo.sort(key=lambda item: item[0].header_offset)
        limit = max(1, min(workers, 4))
        runs = _balanced_runs(todo, limit)
        headers = dict(parts[0].headers) if parts else {}
        stream_dir = dest / DL_DIR / "stream"
        stream_dir.mkdir(parents=True, exist_ok=True)
        stop = Cancel()
        combined = _CombinedCancel(cancel, stop)

        def work(
            run: list[tuple[zipfile.ZipInfo, str]], own: bool
        ) -> None:
            local_reader = main_reader
            local_zf = zf
            try:
                if own:
                    try:
                        local_reader = _stream_reader(parts, headers, combined)
                        local_zf = zipfile.ZipFile(local_reader)
                    except RangeError as exc:
                        raise DirectError(
                            f"cannot read {archive.name} by range: {exc}"
                        ) from exc
                    except (zipfile.BadZipFile, OSError) as exc:
                        raise DirectError(
                            f"cannot open {archive.name} for streaming: {exc}"
                        ) from exc
                for info, relative in run:
                    combined.raise_if_cancelled()
                    fetch_member(info, relative, local_zf, stream_dir, combined)
            finally:
                if own:
                    if local_zf is not None:
                        local_zf.close()
                    if local_reader is not None:
                        local_reader.close()

        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=len(runs)) as pool:
            futures = [
                pool.submit(work, run, index > 0) for index, run in enumerate(runs)
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
                    stop.cancel()
                    for other in futures:
                        other.cancel()
        for exc in errors:
            if not isinstance(exc, Cancelled):
                raise exc
        if errors:
            raise errors[0]

        if total:
            download_module._emit_progress(say, "install", total, total)

    _cleanup_stream_dir(dest)
    _write_done(
        plan,
        archive,
        dest,
        [relative for info, relative in entries if not info.is_dir()],
    )


def _stream_zip_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
) -> list[str] | None:
    """Install ``archive`` by ranged reads straight off the server.

    The zip's central directory is read, every member is validated, files that
    already match are left untouched, and only the rest is fetched. The archive's
    declared md5/sha256 are *not* read or verified here: a whole-archive hash is
    impossible when only the needed ranges are fetched. Integrity rests on each
    member's own CRC-32 plus TLS, and the ``.done`` record stores the
    INDEX-declared hash rather than a hash this path actually confirmed.
    Returns ``None`` when the server does not support range requests, so the
    caller can fall back to the normal download-then-unpack path.
    """
    parts: Sequence[RemoteFile] = archive.parts or (archive.source,)
    headers = dict(parts[0].headers) if parts else {}
    reader: RangeReader | None = None
    zf: zipfile.ZipFile | None = None
    try:
        try:
            reader = _stream_reader(parts, headers, cancel)
        except RangeError:
            return None  # size unknown (HEAD refused) or no ranges: use the normal download
        try:
            zf = zipfile.ZipFile(reader)
        except RangeError as exc:
            if "range" in str(exc).lower():
                return None
            raise DirectError(f"cannot read {archive.name} by range: {exc}") from exc
        except (zipfile.BadZipFile, OSError) as exc:
            raise DirectError(f"cannot open {archive.name}: {exc}") from exc

        entries = _zip_entries(zf)
        for info, _relative in entries:
            if info.flag_bits & 0x1:
                raise DirectError(f"zip member is encrypted: {info.filename!r}")

        _install_streamed_zip(
            plan,
            archive,
            dest,
            zf,
            reader,
            parts,
            entries,
            emit=emit,
            cancel=cancel,
            workers=workers,
        )
        return [relative for info, relative in entries if not info.is_dir()]
    except DirectError:
        raise
    except (RangeError, zipfile.BadZipFile, OSError, zlib.error) as exc:
        raise DirectError(f"cannot stream {archive.name}: {exc}") from exc
    finally:
        if zf is not None:
            zf.close()
        if reader is not None:
            reader.close()


def _stream_7z_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int = 1,
) -> list[str] | None:
    """Install a 7z by reading it straight off the server (no archive on disk).

    Only the headers are read to list the files. A file that already matches (size and
    CRC-32) is left alone, and a solid block whose files all match is never read, so a
    resumed or updated install decodes only what is missing. The rest is decoded
    from the server, each file verified by its CRC as it is written. Independent solid blocks are
    decoded in parallel (``workers`` of them, each over its own connection): one reader decodes
    at the speed of one core and idles the network while it does (HI3: 22 MB/s on an 88 MB/s line).
    Returns ``None`` when the server cannot serve ranges, so the caller can fall back.
    """
    parts: Sequence[RemoteFile] = archive.parts or (archive.source,)
    headers = dict(parts[0].headers) if parts else {}
    reader: RangeReader | None = None
    say = emitter(emit)
    try:
        try:
            reader = _stream_reader(parts, headers, cancel)
        except RangeError:
            return None  # size unknown (HEAD refused) or no ranges: use the normal download
        try:
            entries = sevenz.list_entries(reader)
        except RangeError as exc:
            if "range" in str(exc).lower():
                return None
            raise DirectError(f"cannot read {archive.name} by range: {exc}") from exc
        except sevenz.SevenZipError as exc:
            raise DirectError(f"cannot open {archive.name}: {exc}") from exc

        for entry in entries:
            _safe_destination_path(dest, entry.path)
        wanted: set[str] = set()
        total = 0
        for entry in entries:
            relative = safe_relative_path(entry.path)
            if entry.is_dir:
                _safe_destination_path(dest, relative).mkdir(parents=True, exist_ok=True)
                continue
            if entry.size == 0 and not (dest / relative).is_file():
                wanted.add(entry.path)
                continue
            if not _7z_entry_current(dest / relative, entry):
                wanted.add(entry.path)
                total += entry.size
        diskspace.check_space(dest, total)
        if wanted:
            done_bytes = 0

            def progress(done: int, all_: int) -> None:
                nonlocal done_bytes
                done_bytes = done
                download_module._emit_progress(say, "install", done, all_)

            try:
                sevenz.extract(
                    reader,
                    dest,
                    only=wanted.__contains__,
                    cancel=cancel,
                    progress=progress,
                    workers=max(1, min(workers, _MAX_7Z_WORKERS)),
                    open_reader=lambda: _stream_reader(parts, headers, cancel),
                )
            except sevenz.SevenZipError as exc:
                raise DirectError(f"cannot extract {archive.name}: {exc}") from exc
        return [entry.path for entry in entries if not entry.is_dir]
    except DirectError:
        raise
    except (RangeError, OSError) as exc:
        raise DirectError(f"cannot stream {archive.name}: {exc}") from exc
    finally:
        if reader is not None:
            reader.close()


def _7z_entry_current(path: Path, entry: sevenz.Entry) -> bool:
    """True when ``path`` is the regular file ``entry`` describes (size and CRC-32)."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry.size:
            return False
        if entry.crc is None:
            return True
        return int(_crc32_file(path), 16) == (entry.crc & 0xFFFFFFFF)
    except OSError:
        return False


def _sync_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int = 4,
) -> None:
    _check_work_dirs(dest)
    dl_dir = dest / DL_DIR
    dl_dir.mkdir(parents=True, exist_ok=True)
    source_path = dl_dir / safe_relative_path(archive.source.path)
    part_paths = [dl_dir / safe_relative_path(part.path) for part in archive.parts]
    cache_paths = [] if archive.parts else [source_path]

    if archive.stream and archive.unwrap == ("7z",):
        members = _stream_7z_archive(
            plan, archive, dest, emit=emit, cancel=cancel, workers=workers
        )
        if members is not None:
            try:
                _write_done(plan, archive, dest, members)
            finally:
                _cleanup_stream_dir(dest)
            return
    if archive.stream and archive.unwrap == ("zip",):
        if (
            _stream_zip_archive(
                plan,
                archive,
                dest,
                emit=emit,
                cancel=cancel,
                workers=workers,
            )
            is not None
        ):
            return

    try:
        if archive.parts:
            for part, part_path in zip(archive.parts, part_paths, strict=False):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                _download_remote(part, part_path, emit=emit, cancel=cancel)
            _hash_parts(part_paths, archive.source, archive.name)
            joined = _JoinedFile(part_paths)
            try:
                members = _unwrap(archive, joined, dest, dl_dir, cancel)
            finally:
                joined.close()
        else:
            _download_remote(archive.source, source_path, emit=emit, cancel=cancel)
            members = _unwrap(archive, source_path, dest, dl_dir, cancel)

        for path in cache_paths + part_paths:
            _remove_file(path)
        _write_done(plan, archive, dest, members)
    except (DirectError, zipfile.BadZipFile, ValueError) as exc:
        # The extractors wrap a local write error (a full disk) in DirectError; that
        # leaves the verified archive intact, so keep it for the next sync.
        if not isinstance(exc.__cause__, OSError):
            # A corrupt archive: drop it so the next sync fetches a fresh copy.
            for path in cache_paths + part_paths:
                _remove_file(path)
        raise
    except BaseException:
        # Cancelled, a full disk or any other interruption: keep the verified
        # archive (and any .part) so a later sync can resume instead of refetching.
        raise


_UNWRAPS = frozenset({("zip",), ("zip", "zip"), ("7z",), ("slices",)})


def _is_https_or_loopback(url: Any) -> bool:
    """Production Sophon URLs must be https; the test server is loopback http."""
    if not isinstance(url, str):
        return False
    if url.startswith("https://"):
        return True
    return url.startswith(("http://127.0.0.1:", "http://localhost:"))


def _remove_stale_parts(root: Path, keep: set[str], managed: set[str]) -> None:
    """Best-effort removal of abandoned ``*.part`` files Nadare itself staged in ``root``.

    Only three kinds go: anything inside the download cache, a ``*.nadare-new.part`` (a name
    only Nadare uses), and the ``.part`` of a file the plan manages. Any other ``*.part`` may be
    an archive member or the user's own file, so it stays.
    """
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if not name.endswith(".part"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if rel in keep:
                continue
            staged = rel[: -len(".part")]
            ours = (
                rel.startswith(DL_DIR + "/")
                or staged.endswith(".nadare-new")
                or staged in managed
            )
            if not ours:
                continue
            try:
                path.unlink()
            except OSError:
                pass


def _validate_plan(plan: Plan) -> None:
    seen: set[str] = set()
    for rf in plan.files:
        normalised = safe_relative_path(rf.path)
        if normalised in seen:
            raise DirectError(f"duplicate path in plan: {normalised!r}")
        seen.add(normalised)
    for archive in plan.archives:
        if archive.unwrap not in _UNWRAPS:
            raise DirectError(f"unsupported archive unwrap for {archive.name}: {archive.unwrap!r}")
        safe_relative_path(archive.name)
        safe_relative_path(archive.source.path)
        for part in archive.parts:
            safe_relative_path(part.path)
        if archive.unwrap == ("slices",):
            if not archive.members:
                raise DirectError(f"slices archive {archive.name!r} has no members")
            for member in archive.members:
                safe_relative_path(member.path)
                if member.offset < 0:
                    raise DirectError(f"slice {member.path!r} has a negative offset")
                if member.size < 0:
                    raise DirectError(f"slice {member.path!r} has a negative size")
        elif archive.members:
            raise DirectError(
                f"archive {archive.name!r} has members but is not a slices archive"
            )
    for pkg in plan.sophon:
        safe_relative_path(pkg.name)
        for label, url in (("manifest", pkg.manifest_url), ("chunk", pkg.chunk_url)):
            if not _is_https_or_loopback(url):
                raise DirectError(f"sophon {label} URL is not https: {url!r}")


def safe_relative_path(path: str) -> str:
    """Return ``path`` normalised, or raise :class:`DirectError` for an absolute,
    empty, ``..``-containing or backslash-escaping path."""
    if not isinstance(path, str) or not path:
        raise DirectError("path must not be empty")
    if "\\" in path:
        raise DirectError(f"path must not contain a backslash: {path!r}")
    if path.startswith("/"):
        raise DirectError(f"path must be relative: {path!r}")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise DirectError(f"path must not contain '..': {path!r}")
    if not parts:
        raise DirectError(f"path is empty after normalisation: {path!r}")
    return "/".join(parts)


def plan_missing(
    plan: Plan,
    dest: Path | str,
    *,
    verify: bool = True,
    check_workers: int | None = None,
    cancel: Cancel | None = None,
) -> tuple[list[RemoteFile], list[Archive]]:
    """What :func:`sync` would still fetch: files that are absent or whose size/hash
    differ, and archives with no ``.done`` record for this plan's version.

    ``verify=False`` compares sizes only: right after a sync that already hashed every file.
    File checks run in bounded batches; the returned missing-file list keeps plan order.
    """
    root = Path(dest)
    workers = min(
        max(1, _PLAN_CHECK_MAX_WORKERS if check_workers is None else check_workers),
        _PLAN_CHECK_MAX_WORKERS,
        len(plan.files) or 1,
    )

    def needs_download(rf: RemoteFile) -> bool:
        return not _verify_remote(
            root / safe_relative_path(rf.path), rf, hashes=verify, cancel=cancel
        )

    files: list[RemoteFile] = []
    if workers == 1:
        files.extend(rf for rf in plan.files if needs_download(rf))
    else:
        # Executor.map preserves input order but eagerly submits its inputs in
        # Python 3.12. Submit only one capped batch at a time to bound queued
        # futures and each worker's live file/hash state.
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="nadare-plan-missing"
        ) as pool:
            for offset in range(0, len(plan.files), workers):
                batch = plan.files[offset : offset + workers]
                results = pool.map(needs_download, batch)
                files.extend(rf for rf, missing in zip(batch, results, strict=False) if missing)
    archives = [
        archive
        for archive in plan.archives
        if not _archive_done(plan, archive, root, hashes=verify)
    ]
    return files, archives


def sophon_missing(plan: Plan, dest: Path | str) -> list[sophon.SophonPackage]:
    """Sophon packages with no ``.done`` marker for their manifest id."""
    root = Path(dest)
    return [pkg for pkg in plan.sophon if not sophon.is_done(pkg, root)]


def default_workers(file_count: int) -> int:
    """How many parallel downloads a plan of ``file_count`` loose files deserves.

    A handful of huge files fills the line with a few connections; tens of thousands of small
    ones are latency-bound per connection and need many (Punishing: Gray Raven: 6 workers
    gave 6 MB/s, 32 gave 65-80 MB/s). Capped at 32: 64 gained nothing on a fast line.
    """
    if file_count <= 200:
        return 6
    if file_count <= 2000:
        return 16
    return 32


#: Parallel chunk fetches for a Sophon plan: 100k small requests are latency-bound, like loose
#: files.
_SOPHON_WORKERS = 24


def plan_workers(plan: Plan) -> int:
    """The default number of parallel downloads for ``plan``."""
    if plan.sophon:
        return _SOPHON_WORKERS
    return default_workers(len(plan.files))


@contextmanager
def _install_lock(install_dir: Path) -> Iterator[None]:
    """Refuse to run two direct syncs into one install tree (they share ``.part`` files)."""
    key = hashlib.sha256(str(install_dir.resolve()).encode()).hexdigest()[:16]
    lock_path = paths.state_dir() / "locks" / f"directdl-{key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DirectError(
                f"another install or update is already running for {install_dir}"
            ) from None
        yield
    finally:
        os.close(fd)


def sync(
    plan: Plan,
    dest: Path | str,
    *,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    workers: int = 4,
    check_workers: int | None = None,
) -> dict[str, int]:
    """Install ``plan`` into ``dest``. Returns ``{"downloaded": n, "skipped": m}``
    counting files plus archives. Idempotent: a second run fetches nothing."""
    _validate_plan(plan)
    root = Path(dest).expanduser()
    with _install_lock(root):
        return _sync(
            plan, root, emit=emit, cancel=cancel, workers=workers, check_workers=check_workers
        )


def _sync(
    plan: Plan,
    root: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
    check_workers: int | None = None,
) -> dict[str, int]:
    say = emitter(emit)
    if cancel is not None:
        cancel.raise_if_cancelled()
    _sweep_stale_staging(root)

    missing_files, missing_archives = plan_missing(
        plan, root, check_workers=check_workers, cancel=cancel
    )
    downloaded = 0
    stop = Cancel()
    combined = _CombinedCancel(cancel, stop)

    if missing_files:
        max_workers = max(1, min(workers, MAX_WORKERS))

        def fetch(rf: RemoteFile) -> None:
            combined.raise_if_cancelled()
            _download_remote(
                rf, _safe_destination_path(root, rf.path), emit=emit, cancel=combined
            )

        first_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(fetch, rf) for rf in missing_files]
            for future in as_completed(futures):
                try:
                    future.result()
                    downloaded += 1
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    stop.cancel()
                    for other in futures:
                        other.cancel()
        if first_error is not None:
            raise first_error

    for archive in missing_archives:
        if cancel is not None:
            cancel.raise_if_cancelled()
        say(Event("step", f"installing {archive.name}"))
        _sync_archive(plan, archive, root, emit=emit, cancel=cancel, workers=workers)
        downloaded += 1

    for pkg in plan.sophon:
        if cancel is not None:
            cancel.raise_if_cancelled()
        say(Event("step", f"installing {pkg.name}"))
        try:
            sophon.sync_package(pkg, root, emit=emit, cancel=cancel, workers=workers)
        except sophon.SophonError as exc:
            raise DirectError(str(exc)) from exc
        downloaded += 1

    keep_parts = {safe_relative_path(rf.path) for rf in plan.files}
    can_prune_parts = True
    for pkg in plan.sophon:
        ledger = sophon._ledger_path(pkg, root)
        try:
            raw = ledger.read_text(encoding="utf-8")
            keep_parts.update(
                sophon._validate_ledger_entry(line, ledger)
                for line in raw.splitlines()
                if line
            )
        except (OSError, UnicodeError, sophon.SophonError):
            can_prune_parts = False
            break
    if can_prune_parts:
        _remove_stale_parts(root, keep_parts, {safe_relative_path(rf.path) for rf in plan.files})
    try:
        (root / DL_DIR).rmdir()  # only succeeds when nothing is left in it
    except OSError:
        pass

    skipped = (
        len(plan.files) + len(plan.archives) + len(plan.sophon) - downloaded
    )
    return {"downloaded": downloaded, "skipped": skipped}
