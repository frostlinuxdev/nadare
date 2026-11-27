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
    A file may come compressed (:class:`Packed`, gzip): it is unpacked next to its target and
    checked against the file's own size and hash before it replaces anything.
``archives``
    a game shipped as one zip, as a zip containing a zip, or as a zip split into
    ordered parts. They are downloaded into ``<dest>/.nadare-dl``, verified,
    unwrapped into ``dest`` and then deleted.

Nothing is written outside ``dest``; every path is relative and traversal-free.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed
from concurrent.futures import CancelledError as FutureCancelled
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import (
    archivepins,
    crc64,
    directslices,
    diskspace,
    hdiffsf,
    installlock,
    paths,
    sevenz,
    sophon,
)
from . import download as download_module
from .download import DownloadError, download, remote_size
from .events import Cancel, Cancelled, Emitter, Event, emitter
from .gameversion import versions_equal
from .rangefile import BlockMismatch, RangeError, RangePart, RangeReader

if TYPE_CHECKING:
    from .archivepins import ArchivePin, PinPart

log = logging.getLogger(__name__)

#: Cache directory (under the install dir) for in-flight archive downloads.
#: Most files fetched in parallel. A game with tens of thousands of small files is latency-bound
#: per connection, so one install needs well over 16 connections to fill a fast line.
MAX_WORKERS = 64
#: Most packs on disk in ``.nadare-dl`` at once: pack N unpacks while pack N+1 downloads (#1817).
_MAX_INFLIGHT_PACKS = 2
_PLAN_CHECK_MAX_WORKERS = 16
#: Fewest seconds between two "checked N of M files" reports (#831: ~47,000 files took a
#: minute with nothing on screen). Each report is a logged step line, so not too often.
_CHECK_PROGRESS_INTERVAL = 2.0
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
#: Codecs a :class:`Packed` file may come in (#1557: CZN's dpms chunks are gzip).
PACKED_CODECS = frozenset({"gzip"})
#: Suffix of a packed file's compressed copy next to its target (its resumable download is
#: ``<target>.nadare-gz.part``). Only Nadare uses the name, so a finished sync removes leftovers.
PACKED_SUFFIX = ".nadare-gz"
#: Most bytes a packed file may unpack to when the plan declares no size (a bomb guard).
MAX_UNPACKED_BYTES = 8 << 30


class DirectError(RuntimeError):
    """The plan is invalid, a download failed, or a hash/size did not match.

    Its text reaches the run log and the error dialog, so every URL in it is cut to
    scheme, host and path (no query string, fragment or userinfo).
    """

    def __init__(self, *args: object) -> None:
        args = tuple(
            download_module.public_text(a) if isinstance(a, str) else a for a in args
        )
        super().__init__(*args)


class InstallBusy(DirectError):
    """Another install or update already syncs this folder; nothing was touched."""


@dataclass(frozen=True)
class Packed:
    """The server sends a file compressed (#1557).

    The bytes at ``RemoteFile.url`` are the file compressed with ``codec``; ``size``, ``md5`` and
    ``sha256`` describe those compressed bytes (``None``: not declared). The ``RemoteFile``'s own
    ``size`` and hashes stay the unpacked file's, so every check of an installed tree is the same
    as for a plain file.
    """

    codec: str = "gzip"
    size: int | None = None
    md5: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class RemoteFile:
    """One downloadable object.

    ``path`` is relative to the install dir (for a plain file) or a cache file
    name (for an archive part). ``md5``/``sha256`` are lowercase-insensitive hex,
    ``crc32`` is 8 hex digits. ``headers`` are sent with the request (e.g. Referer).
    ``crc64`` is the whole-file CRC-64/XZ Yostar's manifests list (#1452); ``None`` means
    not declared (0 is a real value: the empty file's). ``packed``: the server sends the file
    compressed (see :class:`Packed`).
    """

    path: str
    url: str
    size: int | None = None
    md5: str | None = None
    sha256: str | None = None
    crc32: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    crc64: int | None = None
    packed: Packed | None = None


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
    #: Published per-block checksums for a streamed install, when the index has one.
    pin: ArchivePin | None = None
    #: The role this archive plays across versions ("client"), for a publisher that renames the
    #: archive every version (Aether Gazer, Brown Dust 2): a record with the same slot and
    #: another name is an earlier version of it, so its dropped files are pruned (#967).
    slot: str = ""
    #: An incremental archive adds to its slot instead of replacing earlier receipts.
    #: A later full archive in the same slot can still supersede it.
    additive: bool = False
    #: Leading folder stripped from every zip member (e.g. ``"game_mini/"``); members outside
    #: it are refused.
    strip_prefix: str = ""


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
    #: Per-hop redirect allow-list for every download of this plan's files and archives
    #: (Yostar keeps downloads on its own hosts, #1452). ``None``: redirects as
    #: :mod:`download` allows them. Not part of equality: two plans that install the same
    #: files are the same plan. A plan with a guard and a streamed archive or Sophon
    #: packages is refused, because those paths could not apply it.
    redirect_allowed: Callable[[str], bool] | None = field(default=None, compare=False)
    #: Loose-file games (#1825): keep a ledger of :attr:`files` (path, size, md5) and, after a
    #: successful sync, remove a file the previous ledger listed that this plan no longer does
    #: (through the same guards as archive members). Off for plans whose file list is partial.
    prune_files: bool = False


class SyncSummary(dict):
    """What :func:`sync` returns: ``{"downloaded": n, "skipped": m}``, plus ``fetched_bytes``.

    ``fetched_bytes`` (an attribute, so the dict compares as before) is what came over the
    network: loose files and packs fetched now, files taken from the predownload left out (#1830).
    """

    def __init__(self, *, downloaded: int, skipped: int, fetched_bytes: int) -> None:
        super().__init__(downloaded=downloaded, skipped=skipped)
        self.fetched_bytes = fetched_bytes


class _OverallProgress:
    """One overall ``done / total`` for a batch of loose files downloaded in parallel (#780).

    Every worker reports its own file's counter, so the raw events jump between 156 MB, 7 KB and
    5 KB totals. This sums them, never goes backwards, and emits at most every ``INTERVAL`` s.
    """

    INTERVAL = 0.25

    def __init__(self, files: Sequence[RemoteFile], emit: Emitter | None) -> None:
        self._say = emitter(emit)
        self._lock = threading.Lock()
        self._file_total = {rf.path: int(_transfer_size(rf) or 0) for rf in files}
        self._file_done = dict.fromkeys(self._file_total, 0)
        self._total = sum(self._file_total.values())
        self._done = 0
        self._cached = 0
        self._last = float("-inf")

    def sink(self, rf: RemoteFile) -> Emitter:
        """The emitter one file's download reports to: progress is summed, the rest passes on."""

        def forward(event: Event) -> None:
            data = event.data if isinstance(event.data, dict) else {}
            if event.kind == "progress" and data.get("stage") == "download":
                self._update(rf.path, int(data.get("done") or 0), int(data.get("total") or 0))
            else:
                self._say(event)

        return forward

    def finished(self, rf: RemoteFile) -> None:
        """A file that needed no download (or just ended) counts as complete."""
        self._update(rf.path, self._file_total[rf.path], self._file_total[rf.path])

    def from_cache(self, rf: RemoteFile) -> None:
        """A file the predownload already had: complete, but not fetched now (#1830)."""
        with self._lock:
            self._cached += self._file_total[rf.path] - self._file_done[rf.path]
        self.finished(rf)

    @property
    def fetched(self) -> int:
        """Bytes that came over the network in this sync, cache hits left out."""
        with self._lock:
            return max(0, self._done - self._cached)

    def _update(self, path: str, done: int, total: int) -> None:
        with self._lock:
            if total > self._file_total[path]:
                self._total += total - self._file_total[path]
                self._file_total[path] = total
            if done > self._file_done[path]:
                self._done += done - self._file_done[path]
                self._file_done[path] = done
            now = time.monotonic()
            if now - self._last >= self.INTERVAL:
                self._last = now
                self._publish()

    def _publish(self) -> None:
        if self._total > 0:
            download_module._emit_progress(self._say, "download", self._done, self._total)

    def flush(self) -> None:
        """Say where things stand now, whatever the throttle."""
        with self._lock:
            self._publish()


class _PackProgress:
    """One overall ``done / total`` across packs, including overlapping downloads.

    Each pack's download reports its own ``done / total``, so with 20 packs (Reverse: 1999,
    31 GiB) the bar filled and said "less than a minute left" 20 times. Each pack's bytes are
    tracked independently and summed out of all packs' planned bytes, so overlapping pack N and
    pack N+1 both move the bar without jumping ahead, going backwards, or exceeding the total.
    A pack that reports another total (a streamed one fetches ranges, not the whole archive) is
    scaled to its planned share.
    """

    def __init__(self, archives: Sequence[Archive], emit: Emitter | None) -> None:
        self._say = emitter(emit)
        self._lock = threading.Lock()
        self._planned_sizes = [_archive_size(archive) for archive in archives]
        self._sizes = [
            0 if _is_streamed_archive(archive) else size
            for archive, size in zip(archives, self._planned_sizes, strict=True)
        ]
        self._pack_done = [0] * len(self._sizes)
        self._total = sum(self._sizes)
        self._done = 0
        self._progress = 0.0
        self._foreground = 0

    def set_foreground(self, index: int) -> None:
        """Mark ``index`` as the foreground pack whose step events belong in the header."""
        with self._lock:
            self._foreground = index

    def finished(self, index: int) -> None:
        """Count pack ``index`` as fully downloaded (including packs reused from cache)."""
        with self._lock:
            size = self._sizes[index]
        if size > 0:
            self._advance(index, size, {"stage": "download"})

    def _advance(self, index: int, done: int, data: Mapping[str, Any]) -> None:
        with self._lock:
            size = self._sizes[index]
            if size <= 0:
                return
            clamped = max(0, min(done, size))
            if clamped <= self._pack_done[index]:
                return
            self._pack_done[index] = clamped
            overall = min(self._total, sum(self._pack_done))
            if overall <= self._done:
                return
            self._done = overall
            self._progress = max(self._progress, overall / self._total)
            sizes = f"{diskspace.human_size(overall)} / {diskspace.human_size(self._total)}"
            self._say(
                Event(
                    "progress",
                    f"download: {sizes}",
                    progress=self._progress,
                    data={**data, "done": overall, "total": self._total},
                )
            )

    def sink(self, index: int) -> Emitter:
        """The emitter pack ``index`` reports to: its download progress adds into overall."""

        def forward(event: Event) -> None:
            data = event.data if isinstance(event.data, dict) else {}
            if event.kind == "step":
                with self._lock:
                    if index != self._foreground:
                        return
                    self._say(event)
                return
            if event.kind != "progress" or data.get("stage") != "download":
                self._say(event)
                return
            done = int(data.get("done") or 0)
            reported = int(data.get("total") or 0)
            with self._lock:
                size = self._sizes[index]
                if size <= 0:
                    adopted = self._planned_sizes[index] or (reported if reported > 0 else 0)
                    if adopted > 0:
                        self._sizes[index] = adopted
                        self._total += adopted
                        size = adopted
                    elif self._total > 0:
                        return
                    else:
                        if done > self._done:
                            self._done = done
                            self._say(event)
                        return
            if reported > 0 and reported != size:
                done = done * size // reported
            self._advance(index, done, data)

        return forward


def _archive_size(archive: Archive) -> int:
    """The bytes an archive's download moves, from the plan (0 when the plan does not say)."""
    if archive.parts:
        return sum(int(_transfer_size(part) or 0) for part in archive.parts)
    return int(_transfer_size(archive.source) or 0)


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


def _caused_by_oserror(exc: BaseException) -> bool:
    """Whether ``exc``, or anything it was raised from, is an ``OSError``.

    ``download()`` wraps a full disk (``ENOSPC``) in a :class:`DownloadError`, which
    :func:`_download_remote` wraps again in a :class:`DirectError`, so the direct cause alone
    cannot tell a full disk (keep the resumable ``.part``) from corrupt bytes (drop them).
    """
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, OSError):
            return True
        seen.add(id(cause))
        cause = cause.__cause__
    return False


def _cache_root_of(path: Path) -> Path | None:
    """The ``.nadare-dl`` folder ``path`` lives under, if any."""
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == DL_DIR:
            return Path(*parts[: index + 1])
    return None


def _refuse_cache_symlinks(path: Path, dl_dir: Path | None = None) -> None:
    """Refuse a cache path that a symlink now stands on, right before Nadare uses it.

    :func:`_cache_path` checks the components that exist while a pack is planned. The write, the
    unpack and the cleanup happen later, on other threads, so a link planted in that gap could
    send a download or an ``unlink`` outside the install folder (#963, SEC F3). Every step that
    touches the cache re-checks here.
    """
    root = dl_dir if dl_dir is not None else _cache_root_of(path)
    if root is None:
        return
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    # The cache folder itself first: lstat follows links in every earlier component, so
    # a replaced .nadare-dl would otherwise pass every check below (Neon r1, packsec).
    probes = [root]
    for part in relative.parts:
        probes.append(probes[-1] / part)
    for probe in probes:
        try:
            mode = probe.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DirectError(f"cannot inspect {probe.name}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise DirectError(
                f"{probe.name} in the download cache is a symlink; remove it and retry"
            )


def _cache_write_guard(path: Path, dl_dir: Path | None = None) -> Callable[[Path], None] | None:
    """One reusable before-write symlink guard for a path inside an active download cache."""
    root = dl_dir if dl_dir is not None else _cache_root_of(path)
    if root is None:
        return None

    def guard(target: Path) -> None:
        _refuse_cache_symlinks(target, root)

    return guard


def _remove_cache_file(path: Path) -> None:
    """Remove one cached download, but never through a link planted below ``.nadare-dl``."""
    try:
        _refuse_cache_symlinks(path)
    except DirectError:
        log.warning(
            "not removing %s: a path component in the download cache is a symlink", path
        )
        return
    _remove_file(path)


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


def _hash_parts(
    paths: list[Path], rf: RemoteFile, name: str, cancel: Cancel | None = None
) -> None:
    """Verify the concatenation of ``paths`` against ``rf`` without joining it."""
    if cancel is not None:
        cancel.raise_if_cancelled()
    size = sum(path.stat().st_size for path in paths)
    algorithms = tuple(alg for alg in ("md5", "sha256") if getattr(rf, alg))
    hashers = {alg: hashlib.new(alg) for alg in algorithms}
    crc = 0
    wide = crc64.Crc64() if rf.crc64 is not None else None
    for path in paths:
        with open(path, "rb") as fh:
            while True:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                block = fh.read(1 << 20)
                if not block:
                    break
                for hasher in hashers.values():
                    hasher.update(block)
                crc = zlib.crc32(block, crc)
                if wide is not None:
                    wide.update(block)
    if cancel is not None:
        cancel.raise_if_cancelled()
    mismatch = rf.size is not None and size != rf.size
    if not mismatch:
        for alg, hasher in hashers.items():
            if hasher.hexdigest().lower() != str(getattr(rf, alg)).lower():
                mismatch = True
                break
    if not mismatch and rf.crc32 and f"{crc & 0xFFFFFFFF:08X}" != rf.crc32.upper():
        mismatch = True
    if not mismatch and wide is not None and wide.value != rf.crc64:
        mismatch = True
    if mismatch:
        raise DirectError(f"archive hash/size mismatch for {name}")


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _crc32_file(path: Path) -> str:
    value = 0
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            value = zlib.crc32(block, value)
    return f"{value & 0xFFFFFFFF:08X}"


def _digest_pass(
    path: Path,
    algorithms: tuple[str, ...],
    want_crc: bool,
    cancel: Cancel | None,
    want_crc64: bool = False,
) -> tuple[dict[str, str], str, int | None]:
    """One read pass over ``path`` for every wanted digest, checking ``cancel`` per block.

    A resumed install re-hashes every file on disk (tens of GB on a NAS), so Cancel has to be
    able to stop it between blocks instead of waiting for the scan to end (#562).
    """
    hashers = {name: hashlib.new(name) for name in algorithms}
    crc = 0
    wide = crc64.Crc64() if want_crc64 else None
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
            if wide is not None:
                wide.update(block)
    digests = {name: hasher.hexdigest() for name, hasher in hashers.items()}
    return digests, f"{crc & 0xFFFFFFFF:08X}", (wide.value if wide is not None else None)


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
        if not algorithms and not rf.crc32 and rf.crc64 is None:
            return True
        digests, crc, wide = _digest_pass(
            path, algorithms, bool(rf.crc32), cancel, want_crc64=rf.crc64 is not None
        )
        for name in algorithms:
            if digests[name].lower() != getattr(rf, name).lower():
                return False
        if rf.crc32 and crc != rf.crc32.upper():
            return False
        if rf.crc64 is not None and wide != rf.crc64:
            return False
    except OSError:
        return False
    return True


def _transfer_size(rf: RemoteFile) -> int | None:
    """The bytes a download of ``rf`` moves: the compressed size for a packed file."""
    return rf.packed.size if rf.packed is not None else rf.size


def _packed_view(rf: RemoteFile) -> RemoteFile:
    """``rf``'s compressed bytes as a file of their own, for :func:`_verify_remote`."""
    packed = rf.packed
    assert packed is not None
    return RemoteFile(
        rf.path, rf.url, packed.size, md5=packed.md5, sha256=packed.sha256, headers=rf.headers
    )


def _declares_hash(rf: RemoteFile) -> bool:
    return bool(rf.md5 or rf.sha256 or rf.crc32 or rf.crc64 is not None)


def _unpack(codec: str, source: Path, target: Path, *, limit: int, cancel: Cancel | None) -> None:
    """Decompress ``source`` into ``target``; more than ``limit`` bytes is refused."""
    if codec != "gzip":  # _validate_plan refuses others; this is the last line
        raise DirectError(f"unsupported packing: {codec!r}")
    written = 0
    with gzip.open(source, "rb") as stream, open(target, "wb") as out:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            block = stream.read(_STREAM_CHUNK)
            if not block:
                break
            written += len(block)
            if written > limit:
                raise DirectError(f"unpacked file is larger than {limit} bytes")
            out.write(block)
        out.flush()
        os.fsync(out.fileno())


def _download_packed(
    rf: RemoteFile,
    path: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    pool: download_module.HttpClientPool | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> None:
    """Fetch a :class:`Packed` file: download the compressed bytes (resumable), check them,
    unpack next to the target, check the file, then replace the target.

    The installed file is never touched until the new one checked out. A cancelled run keeps
    the compressed copy (or its ``.part``) so the next run does not fetch it again.
    """
    packed = rf.packed
    assert packed is not None
    cache_root = _cache_root_of(path)
    write_guard = _cache_write_guard(path, cache_root)
    _refuse_cache_symlinks(path, cache_root)
    if path.exists() and _declares_hash(rf) and _verify_remote(path, rf, cancel=cancel):
        return
    view = _packed_view(rf)
    staged = path.with_name(path.name + PACKED_SUFFIX)
    output = path.with_name(path.name + ".nadare-new.part")
    _refuse_cache_symlinks(staged, cache_root)
    staged_ok = staged.is_file() and _declares_hash(view) and _verify_remote(
        staged, view, cancel=cancel
    )
    need = int(rf.size or 0)
    if not staged_ok and packed.size:
        try:
            have = min(packed.size, Path(f"{staged}.part").stat().st_size)
        except OSError:
            have = 0
        need += packed.size - have
    if need:
        diskspace.check_space(path.parent, need)
    dl_extra: dict[str, Any] = {"before_write": write_guard} if write_guard is not None else {}
    for attempt in range(2):
        if staged_ok:
            break
        _refuse_cache_symlinks(staged, cache_root)
        try:
            download(
                rf.url,
                staged,
                expected_size=packed.size,
                expected_sha256=packed.sha256,
                headers=dict(rf.headers),
                emit=emit,
                cancel=cancel,
                resume=True,
                pool=pool,
                redirect_allowed=redirect_allowed,
                **dl_extra,
            )
        except DownloadError as exc:
            raise DirectError(f"download failed for {rf.path}: {exc}") from (
                download_module.scrub_exception(exc)
            )
        if _verify_remote(staged, view):
            break
        # A wrong resume base (an older .part) or a bad transfer: once more from scratch.
        _remove_cache_file(staged)
        if attempt:
            raise DirectError(f"hash mismatch for {rf.path} (as downloaded)")
    limit = rf.size if rf.size is not None else MAX_UNPACKED_BYTES
    _refuse_cache_symlinks(staged, cache_root)
    _refuse_cache_symlinks(output, cache_root)
    try:
        _unpack(packed.codec, staged, output, limit=limit, cancel=cancel)
    except Cancelled:
        _remove_cache_file(output)
        raise
    except DirectError:
        _remove_cache_file(output)
        _remove_cache_file(staged)
        raise
    except (OSError, EOFError, zlib.error) as exc:
        _remove_cache_file(output)
        _remove_cache_file(staged)
        raise DirectError(f"cannot unpack {rf.path}: {exc}") from exc
    except BaseException:
        _remove_cache_file(output)
        raise
    if not _verify_remote(output, rf):
        _remove_cache_file(output)
        _remove_cache_file(staged)
        raise DirectError(f"hash mismatch for {rf.path}")
    _refuse_cache_symlinks(output, cache_root)
    _refuse_cache_symlinks(path, cache_root)
    os.replace(output, path)
    _remove_cache_file(staged)


def _out_of_space(exc: BaseException) -> bool:
    """Whether ``exc``, or what caused it, is a full disk or an exceeded quota."""
    codes = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in codes:
            return True
        current = current.__cause__ or current.__context__
    return False


def _download_remote(
    rf: RemoteFile,
    path: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    pool: download_module.HttpClientPool | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()
    # The plan-time check in :func:`_cache_path` can be stale by now: re-check before each
    # write-side step, including across remote_size(), _verify_remote() and download() retries.
    cache_root = _cache_root_of(path)
    write_guard = _cache_write_guard(path, cache_root)
    _refuse_cache_symlinks(path, cache_root)
    if rf.packed is not None:
        _download_packed(
            rf, path, emit=emit, cancel=cancel, pool=pool, redirect_allowed=redirect_allowed
        )
        return
    size = rf.size
    if size is None:
        # Some APIs report 0 for a multi-GB archive; ask the server instead of
        # falling back to the downloader's cap for undeclared sizes.
        size = remote_size(rf.url, headers=dict(rf.headers), redirect_allowed=redirect_allowed)
        _refuse_cache_symlinks(path, cache_root)
    preserve_existing = path.exists()
    if (
        preserve_existing
        and (rf.md5 or rf.sha256 or rf.crc32 or rf.crc64 is not None)
        and _verify_remote(path, rf, cancel=cancel)
    ):
        # Already here and verified against a declared hash (e.g. the archive kept after a
        # failed unpack): downloading it again would only double the bytes, and it needs no room.
        return
    temporary = path.with_name(path.name + ".nadare-new")
    destination = temporary if preserve_existing else path
    _refuse_cache_symlinks(destination, cache_root)
    if size:
        # Only what is still to be fetched needs room: a resumed download already holds its
        # .part bytes. A replacement keeps the old file until the new one is verified, so it
        # needs the whole new size (#853).
        have = 0
        if not preserve_existing:
            try:
                have = min(size, destination.with_name(destination.name + ".part").stat().st_size)
            except OSError:
                have = 0
        diskspace.check_space(path.parent, size - have)
    dl_extra: dict[str, Any] = {"before_write": write_guard} if write_guard is not None else {}
    try:
        result = download(
            rf.url,
            destination,
            expected_size=size,
            expected_sha256=rf.sha256,
            headers=dict(rf.headers),
            emit=emit,
            cancel=cancel,
            resume=True,
            pool=pool,
            redirect_allowed=redirect_allowed,
            **dl_extra,
        )
        if rf.md5 or rf.crc32 or rf.crc64 is not None:
            if (
                rf.md5
                and not rf.crc32
                and rf.crc64 is None
                and isinstance(result, download_module.DownloadResult)
                and result.md5
            ):
                if result.md5.lower() != rf.md5.lower():
                    raise DirectError(f"hash mismatch for {rf.path}")
            elif not _verify_remote(destination, rf):
                raise DirectError(f"hash mismatch for {rf.path}")
        if preserve_existing:
            _refuse_cache_symlinks(temporary, cache_root)
            _refuse_cache_symlinks(path, cache_root)
            os.replace(temporary, path)
    except DirectError:
        # A corrupt staged copy must never remove the installed file.
        _remove_cache_file(destination)
        _remove_cache_file(destination.with_name(destination.name + ".part"))
        raise
    except DownloadError as exc:
        # A full disk keeps what arrived, as a plain download does (#1854): once the user
        # makes room, the next try resumes instead of starting over. download() fails before
        # it promotes the .part, so there is no finished staged copy to drop then.
        if preserve_existing and not _out_of_space(exc):
            _remove_cache_file(temporary)
            _remove_cache_file(temporary.with_name(temporary.name + ".part"))
        raise DirectError(f"download failed for {rf.path}: {exc}") from (
            download_module.scrub_exception(exc)
        )
    except BaseException:
        # Cancellation or a local write error keeps the .part for resume.
        raise


#: Most member names (with size and CRC-32) one ``.done`` record keeps.
_MAX_RECORDED_MEMBERS = 100_000


def _done_record(
    plan: Plan,
    archive: Archive,
    members: Sequence[str] | None = None,
    *,
    verified_hashes: bool = True,
    pin: ArchivePin | None = None,
    sequence: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "game": plan.game_tag,
        "version": plan.version,
        "url": archive.source.url,
    }
    if archive.slot:
        record["slot"] = archive.slot
    if archive.additive:
        record["additive"] = True
    if sequence is not None:
        # The order receipts were applied, so a delta's last update wins regardless of mtimes.
        record["sequence"] = sequence
    for name in ("md5", "sha256", "crc32"):
        value = getattr(archive.source, name)
        if value:
            record[name if verified_hashes else f"declared_{name}"] = value.lower()
    if not verified_hashes:
        # A streamed install read only the ranges it needed, so the index's
        # whole-archive hash was never computed. Say what was checked instead.
        record["verified"] = "pin" if pin is not None else "members"
    if pin is not None:
        record["pin_block_size"] = pin.block_size
        record["pin_fingerprint"] = pin.fingerprint()
    if members is not None:
        sorted_members = sorted(set(members))
        recorded_members = sorted_members[:_MAX_RECORDED_MEMBERS]
        record["members"] = recorded_members
        if len(sorted_members) > _MAX_RECORDED_MEMBERS:
            record["members_truncated"] = True
    return record


def _same_version(recorded: object, current: str) -> bool:
    """Whether an archive recorded at ``recorded`` is current for ``current`` (#866)."""
    return versions_equal(recorded, current)


def _archive_done(plan: Plan, archive: Archive, dest: Path, *, hashes: bool = False) -> bool:
    name = safe_relative_path(archive.name)
    record_path = dest / STATE_DIR / f"{name}.done"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    if record.get("url") != archive.source.url:
        return False
    # Check the cheap parts of the record before the expensive member match: a record that
    # already disagrees must not cost a full hash pass over the installed packs.
    if record.get("members_truncated") and not _all_members_present(record_path, dest):
        return False  # the record lists only part of the archive (#906)
    if archive.stream:
        pin = _pin_for(archive)
        if pin is not None and record.get("pin_fingerprint") != pin.fingerprint():
            return False  # installed under another (or no) pin: verify again against this one
    streamed = record.get("verified") in ("members", "pin")
    if streamed and not isinstance(record.get("member_crc32"), dict):
        return False  # a streamed record is only worth as much as its member CRCs
    for name in ("md5", "sha256", "crc32"):
        expected = getattr(archive.source, name)
        actual = record.get(f"declared_{name}" if streamed else name)
        if expected is None:
            if actual not in (None, ""):
                return False
        elif not isinstance(actual, str) or actual.lower() != expected.lower():
            return False
    members = record.get("members")
    member_crcs: dict[str, Any] | None = None
    if members is not None:
        if not isinstance(members, list):
            return False
        member_sizes = record.get("member_sizes")
        member_crcs = record.get("member_crc32")
        if member_sizes is not None and not isinstance(member_sizes, dict):
            return False
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
            except OSError:
                return False
    if not _same_version(record.get("version"), plan.version):
        if not directslices.matches(archive, record, dest, hashes=hashes):
            return False
        if hashes:
            # The manifest moved but this pack is unchanged, and its members were just
            # verified: record the new version so the next check takes the cheap path.
            record["version"] = plan.version
            try:
                _check_work_dirs(dest)
                _write_record(record_path, record)
            except (DirectError, OSError):
                pass  # a failed refresh is harmless: the next check matches again
            member_crcs = None  # the member match already hashed every file
    if hashes and member_crcs is not None and members is not None:
        for member in members:
            expected_crc = member_crcs.get(member)
            if (
                not isinstance(expected_crc, str)
                or _crc32_file(dest / safe_relative_path(member)) != expected_crc.upper()
            ):
                return False
    return True


def _zip_entries(
    zf: zipfile.ZipFile, strip_prefix: str = ""
) -> list[tuple[zipfile.ZipInfo, str]]:
    prefix_dir = safe_relative_path(strip_prefix.rstrip("/")) if strip_prefix else ""
    prefix = f"{prefix_dir}/" if prefix_dir else ""
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    for info in zf.infolist():
        if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
            raise DirectError(f"zip member is a symlink: {info.filename!r}")
        relative = safe_relative_path(info.filename)
        if prefix:
            if relative == prefix_dir and info.is_dir():
                continue
            if not relative.startswith(prefix):
                raise DirectError(f"zip member {info.filename!r} is outside prefix {prefix!r}")
            relative = safe_relative_path(relative[len(prefix) :])
        entries.append((info, relative))
    return entries


class _CancelReader:
    """Wraps a stream so each read first checks the cancel token."""

    def __init__(self, stream: Any, cancel: Cancel | None) -> None:
        self._stream = stream
        self._cancel = cancel

    def read(self, size: int = -1) -> bytes:
        if self._cancel is not None:
            self._cancel.raise_if_cancelled()
        return self._stream.read(size)


def _extract_zip_to(
    zip_path: Path | _JoinedFile,
    target: Path,
    cancel: Cancel | None = None,
    *,
    strip_prefix: str = "",
) -> list[str]:
    """Extract ``zip_path`` into ``target`` and return its relative file paths.

    ``cancel`` is checked between blocks, so a cancelled install stops mid-archive (#929).
    """
    try:
        written: list[str] = []
        with zipfile.ZipFile(zip_path) as zf:
            entries = _zip_entries(zf, strip_prefix)
            target.mkdir(parents=True, exist_ok=True)
            for info, relative in entries:
                out = _safe_destination_path(target, relative, directory=info.is_dir())
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(_CancelReader(src, cancel), dst, 1 << 20)
                written.append(relative)
        return written
    except DirectError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, ValueError) as exc:
        # ValueError: a truncated split pack makes zipfile seek before the start of the
        # joined parts (_JoinedFile), where a plain file would raise OSError (sim-pack 1126).
        name = getattr(zip_path, "name", "archive")
        raise DirectError(f"cannot extract {name}: {exc}") from exc


def _safe_destination_path(
    dest: Path, relative: str, *, directory: bool | None = None
) -> Path:
    """Return a destination path that has no symlinked components below ``dest``.

    ``directory`` says what the caller is about to write there: a file may not replace an
    existing folder (or a folder an existing file), so a hostile plan gets a clear error and
    the item already on disk stays untouched.
    """
    relative = safe_relative_path(relative)
    dest_real = Path(os.path.realpath(dest))
    target = dest / relative
    parts = relative.split("/")
    current = dest
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DirectError(f"cannot inspect install path {relative!r}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise DirectError(f"symlink in install path {relative!r}")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(mode):
                raise DirectError(
                    f"cannot install {relative!r}: {part!r} is not a folder"
                )
        elif directory is True and not stat.S_ISDIR(mode):
            raise DirectError(f"cannot install folder {relative!r}: a file is in the way")
        elif directory is False and not stat.S_ISREG(mode):
            raise DirectError(f"cannot install file {relative!r}: a folder is in the way")
    try:
        parent_real = Path(os.path.realpath(target.parent))
        if os.path.commonpath((str(dest_real), str(parent_real))) != str(dest_real):
            raise DirectError(f"install path escapes root: {relative!r}")
    except ValueError as exc:
        raise DirectError(f"install path escapes root: {relative!r}") from exc
    return target


def _installed_copy_matches(path: Path, expected: tuple[int, str]) -> bool:
    """Whether ``path`` still is the regular file a finished pack recorded (size and CRC-32)."""
    size, crc = expected
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        return _crc32_file(path) == crc.upper()
    except OSError:
        return False


def _keep_installed(
    target: Path, relative: str, keep: Mapping[str, tuple[int, str]] | None
) -> bool:
    """Whether ``target`` already belongs to a later installed pack and must stay untouched."""
    return (
        keep is not None
        and relative in keep
        and _installed_copy_matches(target, keep[relative])
    )


def _merge_tree(
    source: Path,
    dest: Path,
    changed: threading.Event | None = None,
    *,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    """Move every item of ``source`` into ``dest``.

    ``keep`` names files (relative path to the recorded size and CRC-32) a later pack of the
    plan already installed: those stay as they are, because packs are applied in plan order and
    the later pack's copy is the one an uninterrupted sync ends with.
    """
    items = [
        (path, path.relative_to(source).as_posix(), path.is_dir())
        for path in sorted(source.rglob("*"))
    ]
    # Check every item before moving any: a collision must not leave a half-merged install.
    for _path, relative, is_dir in items:
        _safe_destination_path(dest, relative, directory=is_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if changed is not None and items:
        changed.set()  # the first file of the install is about to be replaced (#1826)
    for path, relative, is_dir in items:
        target = _safe_destination_path(dest, relative, directory=is_dir)
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        if _keep_installed(target, relative, keep):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, target)
        except OSError as exc:
            raise DirectError(f"cannot move {relative} into the install: {exc}") from exc


def _extract_zip_into_dest(
    zip_path: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None = None,
    *,
    strip_prefix: str = "",
    changed: threading.Event | None = None,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> list[str]:
    staging = Path(tempfile.mkdtemp(prefix="extract-", dir=dl_dir))
    try:
        members = _extract_zip_to(zip_path, staging, cancel, strip_prefix=strip_prefix)
        if cancel is not None:
            cancel.raise_if_cancelled()  # nothing half-extracted reaches the install
        _merge_tree(staging, dest, changed, keep=keep)
        return members
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_slices_into_dest(
    pack: Path | _JoinedFile,
    members: tuple[Member, ...],
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None = None,
    changed: threading.Event | None = None,
    *,
    keep: Mapping[str, tuple[int, str]] | None = None,
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
            out = _safe_destination_path(staging, relative, directory=False)
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
                    if cancel is not None:
                        cancel.raise_if_cancelled()
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
        if cancel is not None:
            cancel.raise_if_cancelled()
        _merge_tree(staging, dest, changed, keep=keep)
        return [safe_relative_path(member.path) for member in members]
    finally:
        if not joined:
            fh.close()
        shutil.rmtree(staging, ignore_errors=True)


def _extract_7z_into_dest(
    source: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None,
    changed: threading.Event | None = None,
    *,
    keep: Mapping[str, tuple[int, str]] | None = None,
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
        _merge_tree(staging, dest, changed, keep=keep)
        return [entry.path for entry in entries if not entry.is_dir]
    except (sevenz.SevenZipError, OSError) as exc:
        raise DirectError(f"cannot extract the 7z archive: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_hdiff_into_dest(
    source: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None,
    changed: threading.Event | None = None,
    *,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> list[str]:
    """A full-install HDiffPatch diff (Duet Night Abyss), unpacked by :mod:`hdiffsf`."""
    if not isinstance(source, Path):
        raise DirectError("a full-install diff in parts is not supported")
    staging = Path(tempfile.mkdtemp(prefix="extract-", dir=dl_dir))
    try:
        members = hdiffsf.extract(source, staging, cancel=cancel)
        _merge_tree(staging, dest, changed, keep=keep)
        return members
    except hdiffsf.HDiffError as exc:
        raise DirectError(f"cannot unpack {source.name}: {exc}") from exc
    except OSError as exc:
        raise DirectError(f"cannot unpack {source.name}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _unwrap(
    archive: Archive,
    source: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None = None,
    changed: threading.Event | None = None,
    *,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> list[str]:
    if archive.unwrap == ("hdiff",):
        return _extract_hdiff_into_dest(source, dest, dl_dir, cancel, changed, keep=keep)
    if archive.unwrap == ("7z",):
        return _extract_7z_into_dest(source, dest, dl_dir, cancel, changed, keep=keep)
    if archive.unwrap == ("slices",):
        return _extract_slices_into_dest(
            source, archive.members, dest, dl_dir, cancel, changed, keep=keep
        )
    if archive.unwrap == ("zip",):
        return _extract_zip_into_dest(
            source,
            dest,
            dl_dir,
            cancel,
            strip_prefix=archive.strip_prefix,
            changed=changed,
            keep=keep,
        )
    if archive.unwrap != ("zip", "zip"):
        raise DirectError(f"unsupported archive unwrap for {archive.name}: {archive.unwrap!r}")

    outer = Path(tempfile.mkdtemp(prefix="outer-", dir=dl_dir))
    try:
        _extract_zip_to(source, outer, cancel)
        regular = [path for path in outer.rglob("*") if path.is_file()]
        if len(regular) != 1:
            raise DirectError(
                f"nested archive {archive.name} must contain exactly one file, "
                f"found {len(regular)}"
            )
        inner = regular[0]
        if archive.inner_crc32 and _crc32_file(inner) != archive.inner_crc32.upper():
            raise DirectError(f"inner crc32 mismatch for {archive.name}")
        return _extract_zip_into_dest(
            inner,
            dest,
            dl_dir,
            cancel,
            strip_prefix=archive.strip_prefix,
            changed=changed,
            keep=keep,
        )
    finally:
        shutil.rmtree(outer, ignore_errors=True)


def _write_record(record_path: Path, record: Mapping[str, Any]) -> None:
    """Atomically replace one ``.done`` record with ``record``."""
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = record_path.with_name(f"{record_path.name}.tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
    os.replace(temporary, record_path)


def _next_apply_sequence(dest: Path) -> int:
    """One past the highest sequence among this install's receipts."""
    state = dest / STATE_DIR
    highest = 0
    try:
        candidates = state.rglob("*.done")
        for record_path in candidates:
            record = _read_done(record_path)
            if record is None:
                continue
            value = record.get("sequence")
            if isinstance(value, int) and not isinstance(value, bool) and value > highest:
                highest = value
    except OSError:
        pass
    return highest + 1


def _write_done(
    plan: Plan,
    archive: Archive,
    dest: Path,
    members: Sequence[str] | None = None,
    *,
    verified_hashes: bool = True,
    pin: ArchivePin | None = None,
) -> None:
    name = safe_relative_path(archive.name)
    _check_work_dirs(dest)
    record_path = dest / STATE_DIR / f"{name}.done"
    record = _done_record(
        plan,
        archive,
        members,
        verified_hashes=verified_hashes,
        pin=pin,
        sequence=_next_apply_sequence(dest),
    )
    if members is not None:
        record["member_sizes"] = {
            member: (dest / safe_relative_path(member)).stat().st_size
            for member in record["members"]
        }
        record["member_crc32"] = {
            member: _crc32_file(dest / safe_relative_path(member))
            for member in record["members"]
        }
    _write_record(record_path, record)
    _write_member_list(record_path, members if record.get("members_truncated") else None)


def _member_list_path(record_path: Path) -> Path:
    return record_path.with_name(record_path.name.removesuffix(".done") + ".members")


def _write_member_list(record_path: Path, members: Sequence[str] | None) -> None:
    """Keep every member name of an archive whose record is cut at the cap, one per line.

    The record keeps size and CRC for the first members only; this list lets a later check still
    see that every file is there, so a truncated record is not proof of a complete install."""
    sidecar = _member_list_path(record_path)
    if members is None:
        sidecar.unlink(missing_ok=True)
        return
    data = "".join(f"{name}\n" for name in sorted(set(members)))
    fd, temporary_name = tempfile.mkstemp(
        dir=str(sidecar.parent), prefix=f".{sidecar.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, sidecar)
    finally:
        temporary.unlink(missing_ok=True)


def _all_members_present(record_path: Path, dest: Path) -> bool:
    try:
        names = _member_list_path(record_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    if not names:
        return False
    for name in names:
        try:
            if not (dest / safe_relative_path(name)).is_file():
                return False
        except DirectError:
            return False
    return True


def _stream_member_current(path: Path, info: zipfile.ZipInfo) -> bool:
    """True when ``path`` is the regular file described by ``info``."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != info.file_size:
            return False
        return int(_crc32_file(path), 16) == (info.CRC & 0xFFFFFFFF)
    except OSError:
        return False


def _pin_for(archive: Archive) -> ArchivePin | None:
    """The archive's pin, but only when it describes these exact parts."""
    pin = archive.pin
    if pin is None:
        return None
    parts = archive.parts or (archive.source,)
    return pin if pin.matches(parts, archive.source.md5) else None


def _verify_pinned_file(
    archive: Archive, part: PinPart, path: Path, pin: ArchivePin
) -> None:
    """Check a whole downloaded part against its pin, before it is unpacked."""
    try:
        archivepins.verify_file(path, part, pin.block_size)
    except archivepins.PinError as exc:
        raise DirectError(
            f"{archive.name}: the download does not match the published checksum list "
            f"({exc}). Nothing was installed: try again in a few minutes, and report it "
            "if it keeps happening."
        ) from exc


def _pin_mismatch_error(archive: Archive, exc: BlockMismatch) -> DirectError:
    total = len(archive.parts or (archive.source,))
    return DirectError(
        f"{archive.name}: the downloaded data does not match Nadare's checksum list "
        f"(part {exc.part_index + 1} of {total}, block {exc.block_index}, {exc.url}). "
        "Nothing from that block was installed. The publisher may have just updated "
        "the file: try again in a few minutes, and report it if it keeps happening."
    )


def _pin_needs_ranges(archive: Archive, exc: BaseException) -> DirectError:
    """A pinned archive is never installed from bytes that skipped the block checks."""
    return DirectError(
        f"{archive.name}: the server did not give the ranged reads needed to check the "
        f"download against Nadare's checksum list ({exc}). Nothing was installed; try again later."
    )


def _stream_reader(
    parts: Sequence[RemoteFile],
    headers: Mapping[str, str],
    cancel: Cancel | None,
    pin: ArchivePin | None = None,
) -> RangeReader:
    ranges = [
        RangePart(rf.url, rf.size if rf.size is not None or pin is None else pin_part.size)
        for rf, pin_part in zip(parts, pin.parts if pin is not None else parts, strict=True)
    ]
    return RangeReader(
        ranges,
        headers=headers,
        block_size=pin.block_size if pin is not None else _STREAM_BLOCK,
        block_hashes=pin.block_hashes() if pin is not None else None,
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


def _cache_path(dl_dir: Path, relative: str) -> Path:
    """The cache file for a validated relative archive path, refusing any symlink on the way.

    ``_check_work_dirs`` only looks at ``.nadare-dl`` itself. An archive name may hold folders
    (Brown Dust 2 uses the publisher's relative file name), and a link planted at one of them, or
    at the file or its ``.part``, would send the download and the cleanup ``unlink`` outside the
    install folder (#963).
    """
    safe = safe_relative_path(relative)
    probe = dl_dir
    for part in Path(safe).parts:
        probe = probe / part
        for candidate in (probe, Path(f"{probe}.part")):
            try:
                mode = candidate.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DirectError(f"cannot inspect {candidate.name}: {exc}") from exc
            if stat.S_ISLNK(mode):
                raise DirectError(
                    f"{candidate.name} in the download cache is a symlink; remove it and retry"
                )
    return dl_dir / safe


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
    pin: ArchivePin | None = None,
    changed: threading.Event | None = None,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    """Write the members of an already-open remote zip into ``dest``."""
    for info, relative in entries:
        if info.is_dir():
            _safe_destination_path(dest, relative, directory=True).mkdir(
                parents=True, exist_ok=True
            )

    todo: list[tuple[zipfile.ZipInfo, str]] = []
    for info, relative in entries:
        if info.is_dir():
            continue
        target = _safe_destination_path(dest, relative, directory=False)
        if _keep_installed(target, relative, keep):
            continue
        if not _stream_member_current(target, info):
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
            out = _safe_destination_path(dest, relative, directory=False)
            out.parent.mkdir(parents=True, exist_ok=True)
            out = _safe_destination_path(dest, relative, directory=False)
            if changed is not None:
                changed.set()  # this member replaces a file of the install (#1826)
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
                        local_reader = _stream_reader(parts, headers, combined, pin=pin)
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
        with ThreadPoolExecutor(
            initializer=download_module.inherit_transfer,
            initargs=(download_module.current_transfer(),),
            max_workers=len(runs),
        ) as pool:
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
        # A queued task cancelled to stop the rest raises FutureCancelled: that is the user's
        # cancel (or the first failure) taking effect, never an error of its own.
        for exc in errors:
            if not isinstance(exc, Cancelled | FutureCancelled):
                raise exc
        if errors:
            raise next((e for e in errors if isinstance(e, Cancelled)), Cancelled())

        if total:
            download_module._emit_progress(say, "install", total, total)

    _cleanup_stream_dir(dest)
    _write_done(
        plan,
        archive,
        dest,
        [relative for info, relative in entries if not info.is_dir()],
        verified_hashes=False,
        pin=pin,
    )


def _stream_zip_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
    pin: ArchivePin | None = None,
    changed: threading.Event | None = None,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> list[str] | None:
    """Install ``archive`` by ranged reads straight off the server.

    The zip's central directory is read, every member is validated, files that
    already match are left untouched, and only the rest is fetched. The archive's
    declared md5/sha256 are *not* verified here: a whole-archive hash is
    impossible when only the needed ranges are fetched. Integrity rests on each
    member's own CRC-32 plus TLS, so the ``.done`` record keeps the declared
    hash under ``declared_*`` and marks the member CRCs as the verified part.
    An archive with a declared sha256 does not come this way: the caller falls
    back to the normal download, which checks the whole archive.
    Returns ``None`` when the server does not support range requests, so the
    caller can fall back to the normal download-then-unpack path.
    """
    parts: Sequence[RemoteFile] = archive.parts or (archive.source,)
    headers = dict(parts[0].headers) if parts else {}
    reader: RangeReader | None = None
    zf: zipfile.ZipFile | None = None
    try:
        try:
            reader = _stream_reader(parts, headers, cancel, pin=pin)
        except RangeError as exc:
            if pin is not None:
                raise _pin_needs_ranges(archive, exc) from exc
            return None  # size unknown (HEAD refused) or no ranges: use the normal download
        try:
            zf = zipfile.ZipFile(reader)
        except RangeError as exc:
            if pin is not None:
                raise _pin_needs_ranges(archive, exc) from exc
            if "range" in str(exc).lower():
                return None
            raise DirectError(f"cannot read {archive.name} by range: {exc}") from exc
        except (zipfile.BadZipFile, OSError) as exc:
            raise DirectError(f"cannot open {archive.name}: {exc}") from exc

        entries = _zip_entries(zf, archive.strip_prefix)
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
            pin=pin,
            changed=changed,
            keep=keep,
        )
        return [relative for info, relative in entries if not info.is_dir()]
    except DirectError:
        raise
    except BlockMismatch as exc:
        raise _pin_mismatch_error(archive, exc) from exc
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
    pin: ArchivePin | None = None,
    changed: threading.Event | None = None,
    keep: Mapping[str, tuple[int, str]] | None = None,
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
            reader = _stream_reader(parts, headers, cancel, pin=pin)
        except RangeError as exc:
            if pin is not None:
                raise _pin_needs_ranges(archive, exc) from exc
            return None  # size unknown (HEAD refused) or no ranges: use the normal download
        try:
            entries = sevenz.list_entries(reader)
        except RangeError as exc:
            if pin is not None:
                raise _pin_needs_ranges(archive, exc) from exc
            if "range" in str(exc).lower():
                return None
            raise DirectError(f"cannot read {archive.name} by range: {exc}") from exc
        except sevenz.SevenZipError as exc:
            raise DirectError(f"cannot open {archive.name}: {exc}") from exc

        for entry in entries:
            _safe_destination_path(dest, entry.path, directory=entry.is_dir)
        wanted: set[str] = set()
        total = 0
        for entry in entries:
            relative = safe_relative_path(entry.path)
            if entry.is_dir:
                _safe_destination_path(dest, relative, directory=True).mkdir(
                    parents=True, exist_ok=True
                )
                continue
            target = dest / relative
            if _keep_installed(target, relative, keep):
                continue
            if entry.size == 0 and not target.is_file():
                wanted.add(entry.path)
                continue
            if not _7z_entry_current(target, entry):
                wanted.add(entry.path)
                total += entry.size
        diskspace.check_space(dest, total)
        if wanted:
            done_bytes = 0

            def progress(done: int, all_: int) -> None:
                nonlocal done_bytes
                done_bytes = done
                if done and changed is not None:
                    changed.set()  # members are being written into the install (#1826)
                download_module._emit_progress(say, "install", done, all_)

            try:
                sevenz.extract(
                    reader,
                    dest,
                    only=wanted.__contains__,
                    cancel=cancel,
                    progress=progress,
                    workers=max(1, min(workers, _MAX_7Z_WORKERS)),
                    open_reader=lambda: _stream_reader(parts, headers, cancel, pin=pin),
                )
            except sevenz.SevenZipError as exc:
                raise DirectError(f"cannot extract {archive.name}: {exc}") from exc
        return [entry.path for entry in entries if not entry.is_dir]
    except DirectError:
        raise
    except BlockMismatch as exc:
        raise _pin_mismatch_error(archive, exc) from exc
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


def _is_streamed_archive(archive: Archive) -> bool:
    whole_sha256 = bool(archive.source.sha256) or any(p.sha256 for p in archive.parts)
    return archive.stream and not whole_sha256 and archive.unwrap in (("7z",), ("zip",))


def _archive_remaining_bytes(archive: Archive, dl_dir: Path) -> int:
    """Bytes of ``archive`` still to be downloaded into ``dl_dir`` (refusing symlinks)."""
    try:
        dl_mode = dl_dir.lstat().st_mode
    except FileNotFoundError:
        dl_mode = None
    except OSError as exc:
        raise DirectError(f"cannot inspect {dl_dir.name}: {exc}") from exc
    if dl_mode is not None and stat.S_ISLNK(dl_mode):
        raise DirectError(f"{dl_dir.name} in the install folder is a symlink; remove it and retry")
    items = archive.parts or (archive.source,)
    need = 0
    for rf in items:
        target = _cache_path(dl_dir, rf.path)
        size = int(_transfer_size(rf) or 0)
        if size <= 0:
            continue
        have = 0
        for candidate in (target, Path(f"{target}.part")):
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                have = min(size, info.st_size)
                break
        need += max(0, size - have)
    return need


def _pack_space_needed(archives: Sequence[Archive], dl_dir: Path) -> int:
    """Peak disk room for the packs in flight, including the one being unpacked.

    Up to :data:`_MAX_INFLIGHT_PACKS` archives are in ``.nadare-dl`` at once, and while pack N
    unpacks the extractor writes a second, unpacked copy into a staging folder before merging
    it. The plan does not know that unpacked size, so the pack's own download size stands in
    for it: exact for a stored zip, a floor for a compressed one (SEC F2). Streamed archives
    drain in-flight packs before running, so packs on opposite sides of one never overlap.
    """
    window = max(1, _MAX_INFLIGHT_PACKS)
    peak = 0
    #: ``(bytes still to download, full download size)`` per pack, in plan order.
    run: list[tuple[int, int]] = []

    def finish_run() -> None:
        nonlocal peak
        if not run:
            return
        # Every pack unpacks once, with the packs after it (up to the window) on disk: the last
        # one of a run, and a run of one, unpack alone but still need their staging copy (#1854).
        for start in range(len(run)):
            chunk = run[start : start + window]
            staging = chunk[0][1]
            peak = max(peak, sum(remaining for remaining, _size in chunk) + staging)
        run.clear()

    for archive in archives:
        if _is_streamed_archive(archive):
            finish_run()
        else:
            run.append((_archive_remaining_bytes(archive, dl_dir), _archive_size(archive)))
    finish_run()
    return peak


def _peak_archive_concurrency(archives: Sequence[Archive]) -> int:
    """Peak archive files/parts that may download concurrently across in-flight packs."""
    window = max(1, _MAX_INFLIGHT_PACKS)
    peak = 0
    run: list[int] = []

    def finish_run() -> None:
        nonlocal peak
        if not run:
            return
        if len(run) <= window:
            peak = max(peak, sum(run))
        else:
            for i in range(len(run) - window + 1):
                peak = max(peak, sum(run[i : i + window]))
        run.clear()

    for archive in archives:
        items = max(1, len(archive.parts))
        if _is_streamed_archive(archive):
            finish_run()
            peak = max(peak, items)
        else:
            run.append(items)
    finish_run()
    return peak


def _record_hashes_match(archive: Archive, record: Mapping[str, Any]) -> bool:
    """Whether ``record``'s recorded source hashes agree with ``archive.source``."""
    streamed = record.get("verified") in ("members", "pin")
    for name in ("md5", "sha256", "crc32"):
        expected = getattr(archive.source, name)
        actual = record.get(f"declared_{name}" if streamed else name)
        if expected is None:
            if actual not in (None, ""):
                return False
        elif not isinstance(actual, str) or actual.lower() != expected.lower():
            return False
    return True


def _later_member_owners(
    plan: Plan, archive: Archive, root: Path, *, installing: Sequence[Archive] = ()
) -> dict[str, tuple[int, str]]:
    """Members that packs *later* than ``archive`` in plan order already installed.

    A later pack in ``installing`` is laid down again by this sync, so its receipt is from an
    older build and says nothing about what it ships now (Neon r1, packsec).

    A resumed sync installs only the packs that are missing. If an earlier missing pack ships a
    path that a later pack already installed, the later pack's copy must stay: packs apply in
    plan order, so it is the copy an uninterrupted sync ends with (sim-pack F2). Each entry is
    the recorded ``(size, crc32)`` the installed file must still match.
    """
    try:
        index = next(i for i, item in enumerate(plan.archives) if item is archive)
    except StopIteration:
        return {}
    owners: dict[str, tuple[int, str]] = {}
    reinstalled = {id(item) for item in installing}
    for later in plan.archives[index + 1 :]:
        if id(later) in reinstalled:
            continue
        record_path = root / STATE_DIR / f"{safe_relative_path(later.name)}.done"
        record = _read_done(record_path)
        if record is None or record.get("url") != later.source.url:
            continue
        if record.get("members_truncated"):
            continue
        if not _same_version(record.get("version"), plan.version):
            if not (
                _record_hashes_match(later, record)
                and directslices.matches(later, record, root, hashes=False)
            ):
                continue
        members = record.get("members")
        sizes = record.get("member_sizes")
        crcs = record.get("member_crc32")
        if not (isinstance(members, list) and isinstance(sizes, dict) and isinstance(crcs, dict)):
            continue
        for member in members:
            if not isinstance(member, str):
                continue
            size, crc = sizes.get(member), crcs.get(member)
            if isinstance(size, int) and not isinstance(size, bool) and isinstance(crc, str):
                owners[member] = (size, crc)
    return owners


def _unpack_with_keep(
    archive: Archive,
    source: Path | _JoinedFile,
    dest: Path,
    dl_dir: Path,
    cancel: Cancel | None,
    changed: threading.Event | None,
    keep: Mapping[str, tuple[int, str]] | None,
) -> list[str]:
    """Call :func:`_unwrap` with the later-owner map when there is one."""
    if keep:
        return _unwrap(archive, source, dest, dl_dir, cancel, changed, keep=keep)
    return _unwrap(archive, source, dest, dl_dir, cancel, changed)


def _unpack_downloaded_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    dl_dir: Path,
    source_path: Path,
    part_paths: list[Path],
    cache_paths: list[Path],
    pin: ArchivePin | None,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    changed: threading.Event | None,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    # A link planted under .nadare-dl after the download must not send the unpack outside
    # the install either (SEC F3).
    for cached in (source_path, *part_paths):
        _refuse_cache_symlinks(cached, dl_dir)
    try:
        if archive.parts:
            joined = _JoinedFile(part_paths)
            try:
                _say_unpacking(archive, emit)
                members = _unpack_with_keep(
                    archive, joined, dest, dl_dir, cancel, changed, keep
                )
            finally:
                joined.close()
        else:
            _say_unpacking(archive, emit)
            members = _unpack_with_keep(
                archive, source_path, dest, dl_dir, cancel, changed, keep
            )
        for path in cache_paths + part_paths:
            _remove_cache_file(path)
        _write_done(plan, archive, dest, members, pin=pin)
    except (DirectError, zipfile.BadZipFile, ValueError) as exc:
        # The extractors wrap a local write error (a full disk) in DirectError; that
        # leaves the verified archive intact, so keep it for the next sync.
        if not _caused_by_oserror(exc):
            # A corrupt archive: drop it so the next sync fetches a fresh copy.
            for path in cache_paths + part_paths:
                _remove_cache_file(path)
        raise


def _sync_archive(
    plan: Plan,
    archive: Archive,
    dest: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int = 4,
    changed: threading.Event | None = None,
    pool: download_module.HttpClientPool | None = None,
    keep: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    _check_work_dirs(dest)
    dl_dir = dest / DL_DIR
    dl_dir.mkdir(parents=True, exist_ok=True)
    source_path = _cache_path(dl_dir, archive.source.path)
    part_paths = [_cache_path(dl_dir, part.path) for part in archive.parts]
    cache_paths = [] if archive.parts else [source_path]

    # A whole-archive sha256 cannot be computed from ranged reads, and a streamed
    # install must not record the index hash as if it had checked it. Fall through
    # to the normal download-then-unpack path, which verifies the whole archive.
    whole_sha256 = bool(archive.source.sha256) or any(p.sha256 for p in archive.parts)
    pin = _pin_for(archive)
    if archive.stream and not whole_sha256 and archive.unwrap == ("7z",):
        members = _stream_7z_archive(
            plan,
            archive,
            dest,
            emit=emit,
            cancel=cancel,
            workers=workers,
            pin=pin,
            changed=changed,
            keep=keep,
        )
        if members is not None:
            try:
                _write_done(plan, archive, dest, members, verified_hashes=False, pin=pin)
            finally:
                _cleanup_stream_dir(dest)
            return
    if archive.stream and not whole_sha256 and archive.unwrap == ("zip",):
        if (
            _stream_zip_archive(
                plan,
                archive,
                dest,
                emit=emit,
                cancel=cancel,
                workers=workers,
                pin=pin,
                changed=changed,
                keep=keep,
            )
            is not None
        ):
            return

    try:
        if archive.parts:
            stop = Cancel()
            combined = _CombinedCancel(cancel, stop)
            overall = _OverallProgress(archive.parts, emit)
            own_pool = pool is None
            connections = pool if pool is not None else download_module.HttpClientPool()
            release_thread = None if own_pool else getattr(connections, "release_thread", None)

            def fetch_part(item):
                index, part, part_path = item
                ok = False
                try:
                    combined.raise_if_cancelled()
                    _download_remote(
                        part,
                        part_path,
                        emit=overall.sink(part),
                        cancel=combined,
                        pool=connections,
                        redirect_allowed=plan.redirect_allowed,
                    )
                    if pin is not None:
                        _verify_pinned_file(archive, pin.parts[index], part_path, pin)
                    overall.finished(part)
                    ok = True
                finally:
                    if release_thread is not None:
                        release_thread(reuse=ok)

            executor = ThreadPoolExecutor(
                initializer=download_module.inherit_transfer,
                initargs=(download_module.current_transfer(),),
                max_workers=max(1, min(workers, MAX_WORKERS)),
            )
            futures: list[Any] = []
            try:
                futures = [
                    executor.submit(fetch_part, (i, part, path))
                    for i, (part, path) in enumerate(
                        zip(archive.parts, part_paths, strict=True)
                    )
                ]
                pending = set(futures)
                poll_timeout = 0.05 if cancel is not None else None
                while pending:
                    combined.raise_if_cancelled()
                    done_set, pending = wait_futures(
                        pending, timeout=poll_timeout, return_when=FIRST_COMPLETED
                    )
                    if done_set:
                        for future in futures:
                            if future in done_set:
                                future.result()
            except BaseException:
                stop.cancel()
                for future in futures:
                    future.cancel()
                if own_pool:
                    # Close sockets before waiting on workers so a stalled read wakes at once.
                    connections.close()
                raise
            finally:
                executor.shutdown(wait=True)
                if own_pool:
                    connections.close()
            overall.flush()
            _hash_parts(part_paths, archive.source, archive.name, cancel=cancel)
        else:
            release_thread = None if pool is None else getattr(pool, "release_thread", None)
            ok = False
            try:
                _download_remote(
                    archive.source,
                    source_path,
                    emit=emit,
                    cancel=cancel,
                    pool=pool,
                    redirect_allowed=plan.redirect_allowed,
                )
                if pin is not None:
                    _verify_pinned_file(archive, pin.parts[0], source_path, pin)
                ok = True
            finally:
                if release_thread is not None:
                    release_thread(reuse=ok)
        _unpack_downloaded_archive(
            plan,
            archive,
            dest,
            dl_dir,
            source_path,
            part_paths,
            cache_paths,
            pin,
            emit=emit,
            cancel=cancel,
            changed=changed,
            keep=keep,
        )
    except (DirectError, zipfile.BadZipFile, ValueError) as exc:
        if not _caused_by_oserror(exc):
            for path in cache_paths + part_paths:
                _remove_cache_file(path)
        raise
    except BaseException:
        # Cancelled, a full disk or any other interruption: keep the verified
        # archive (and any .part) so a later sync can resume instead of refetching.
        raise


@dataclass
class _PackSlot:
    index: int
    archive: Archive
    verified: bool = False
    streamed: bool = False
    error: BaseException | None = None
    dl_dir: Path | None = None
    source_path: Path | None = None
    part_paths: list[Path] = field(default_factory=list)
    cache_paths: list[Path] = field(default_factory=list)
    pin: ArchivePin | None = None


def _sync_archives(
    plan: Plan,
    missing_archives: Sequence[Archive],
    root: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
    changed: threading.Event | None,
) -> int:
    """Install ``missing_archives`` with a 2-pack download/unpack pipeline (#1817).

    Pack N+1 downloads on a shared keep-alive pool and worker pool while pack N finishes its
    tail, verifies and unpacks. At most :data:`_MAX_INFLIGHT_PACKS` packs occupy ``.nadare-dl``
    at once, and unpacking runs strictly in plan order on the calling thread.
    """
    if not missing_archives:
        return 0
    if cancel is not None:
        cancel.raise_if_cancelled()

    say = emitter(emit)
    if len(missing_archives) == 1:
        archive = missing_archives[0]
        pack_progress = _PackProgress(missing_archives, emit)
        say(Event("step", f"installing pack 1 of 1: {archive.name}"))
        _sync_archive(
            plan,
            archive,
            root,
            emit=pack_progress.sink(0),
            cancel=cancel,
            workers=workers,
            changed=changed,
            keep=_later_member_owners(plan, archive, root, installing=missing_archives),
        )
        return 1

    _check_work_dirs(root)
    needed = _pack_space_needed(missing_archives, root / DL_DIR)
    if needed:
        diskspace.check_space(root, needed)

    pack_progress = _PackProgress(missing_archives, emit)
    stop = Cancel()
    combined = _CombinedCancel(cancel, stop)
    slots = [_PackSlot(index, archive) for index, archive in enumerate(missing_archives)]
    state_cond = threading.Condition()
    in_flight = 0
    first_dl_error: list[BaseException] = []
    submitted_futures: list[Any] = []
    max_workers = max(1, min(workers, MAX_WORKERS))
    connections = download_module.HttpClientPool()

    def record_dl_error(slot: _PackSlot | None, exc: BaseException) -> None:
        with state_cond:
            if slot is not None and slot.error is None:
                slot.error = exc
            if not isinstance(exc, Cancelled | FutureCancelled) and not first_dl_error:
                first_dl_error.append(exc)
            stop.cancel()
            for fut in submitted_futures:
                fut.cancel()
            state_cond.notify_all()

    def download_single(slot: _PackSlot) -> None:
        assert slot.source_path is not None
        try:
            combined.raise_if_cancelled()
            _download_remote(
                slot.archive.source,
                slot.source_path,
                emit=pack_progress.sink(slot.index),
                cancel=combined,
                pool=connections,
                redirect_allowed=plan.redirect_allowed,
            )
            if slot.pin is not None:
                _verify_pinned_file(slot.archive, slot.pin.parts[0], slot.source_path, slot.pin)
            combined.raise_if_cancelled()
            pack_progress.finished(slot.index)
            with state_cond:
                combined.raise_if_cancelled()
                slot.verified = True
                state_cond.notify_all()
        except BaseException as exc:
            if isinstance(exc, DirectError | zipfile.BadZipFile | ValueError):
                if not _caused_by_oserror(exc):
                    for path in slot.cache_paths:
                        _remove_cache_file(path)
            record_dl_error(slot, exc)

    def download_part(
        slot: _PackSlot,
        part_idx: int,
        part: RemoteFile,
        part_path: Path,
        overall: _OverallProgress,
        remaining: list[int],
        part_lock: threading.Lock,
    ) -> None:
        try:
            combined.raise_if_cancelled()
            _download_remote(
                part,
                part_path,
                emit=overall.sink(part),
                cancel=combined,
                pool=connections,
                redirect_allowed=plan.redirect_allowed,
            )
            if slot.pin is not None:
                try:
                    _verify_pinned_file(slot.archive, slot.pin.parts[part_idx], part_path, slot.pin)
                except DirectError:
                    _remove_cache_file(part_path)
                    raise
            combined.raise_if_cancelled()
            overall.finished(part)
            with part_lock:
                remaining[0] -= 1
                is_last = remaining[0] == 0
            if is_last:
                overall.flush()
                combined.raise_if_cancelled()
                try:
                    _hash_parts(
                        slot.part_paths, slot.archive.source, slot.archive.name, cancel=combined
                    )
                except DirectError:
                    for path in slot.part_paths:
                        _remove_cache_file(path)
                    raise
                combined.raise_if_cancelled()
                pack_progress.finished(slot.index)
                with state_cond:
                    combined.raise_if_cancelled()
                    slot.verified = True
                    state_cond.notify_all()
        except BaseException as exc:
            record_dl_error(slot, exc)

    def feed(dl_pool: ThreadPoolExecutor) -> None:
        nonlocal in_flight
        current_slot: _PackSlot | None = None
        try:
            for slot in slots:
                current_slot = slot
                streamed = _is_streamed_archive(slot.archive)
                with state_cond:
                    limit = 0 if streamed else _MAX_INFLIGHT_PACKS - 1
                    while in_flight > limit and not combined.cancelled:
                        state_cond.wait(timeout=0.05)
                    if combined.cancelled:
                        return
                    in_flight += 1
                    if streamed:
                        slot.streamed = True
                        state_cond.notify_all()
                        while in_flight > 0 and not combined.cancelled:
                            state_cond.wait(timeout=0.05)
                        continue

                _check_work_dirs(root)
                dl_dir = root / DL_DIR
                dl_dir.mkdir(parents=True, exist_ok=True)
                slot.dl_dir = dl_dir
                slot.source_path = _cache_path(dl_dir, slot.archive.source.path)
                slot.part_paths = [_cache_path(dl_dir, p.path) for p in slot.archive.parts]
                slot.cache_paths = [] if slot.archive.parts else [slot.source_path]
                slot.pin = _pin_for(slot.archive)

                if slot.archive.parts:
                    overall = _OverallProgress(slot.archive.parts, pack_progress.sink(slot.index))
                    remaining = [len(slot.archive.parts)]
                    part_lock = threading.Lock()
                    with state_cond:
                        if combined.cancelled:
                            return
                        for part_idx, (part, part_path) in enumerate(
                            zip(slot.archive.parts, slot.part_paths, strict=True)
                        ):
                            submitted_futures.append(
                                dl_pool.submit(
                                    download_part,
                                    slot,
                                    part_idx,
                                    part,
                                    part_path,
                                    overall,
                                    remaining,
                                    part_lock,
                                )
                            )
                else:
                    with state_cond:
                        if combined.cancelled:
                            return
                        submitted_futures.append(dl_pool.submit(download_single, slot))
        except BaseException as exc:
            record_dl_error(current_slot, exc)
        finally:
            with state_cond:
                state_cond.notify_all()

    unpack_error: BaseException | None = None
    downloaded = 0
    dl_pool = ThreadPoolExecutor(
        initializer=download_module.inherit_transfer,
        initargs=(download_module.current_transfer(),),
        max_workers=max_workers,
    )
    try:
        feeder = threading.Thread(
            target=feed, args=(dl_pool,), name="nadare-pack-feeder", daemon=True
        )
        feeder.start()
        try:
            for number, slot in enumerate(slots, 1):
                archive = slot.archive
                if cancel is not None:
                    cancel.raise_if_cancelled()
                with state_cond:
                    emit_step = slot.error is None and not stop.cancelled
                if emit_step:
                    say(
                        Event(
                            "step",
                            f"installing pack {number} of {len(slots)}: {archive.name}",
                        )
                    )
                pack_progress.set_foreground(slot.index)
                while True:
                    with state_cond:
                        if (
                            slot.verified
                            or slot.streamed
                            or slot.error is not None
                            or combined.cancelled
                        ):
                            slot_error = slot.error
                            is_verified = slot.verified
                            is_streamed = slot.streamed
                            break
                        state_cond.wait(timeout=0.05)
                if cancel is not None and cancel.cancelled:
                    stop.cancel()
                    with state_cond:
                        for fut in submitted_futures:
                            fut.cancel()
                        state_cond.notify_all()
                    cancel.raise_if_cancelled()
                if slot_error is not None:
                    raise slot_error
                if stop.cancelled:
                    break
                if not (is_verified or is_streamed):
                    break
                if is_streamed:
                    _sync_archive(
                        plan,
                        archive,
                        root,
                        emit=pack_progress.sink(slot.index),
                        cancel=cancel,
                        workers=workers,
                        changed=changed,
                        pool=connections,
                        keep=_later_member_owners(plan, archive, root, installing=missing_archives),
                    )
                else:
                    assert slot.dl_dir is not None and slot.source_path is not None
                    _unpack_downloaded_archive(
                        plan,
                        archive,
                        root,
                        slot.dl_dir,
                        slot.source_path,
                        slot.part_paths,
                        slot.cache_paths,
                        slot.pin,
                        emit=emit,
                        cancel=cancel,
                        changed=changed,
                        keep=_later_member_owners(plan, archive, root, installing=missing_archives),
                    )
                downloaded += 1
                with state_cond:
                    in_flight -= 1
                    state_cond.notify_all()
        except BaseException as exc:
            unpack_error = exc
            stop.cancel()
            with state_cond:
                for fut in submitted_futures:
                    fut.cancel()
                state_cond.notify_all()
        finally:
            stop.cancel()
            with state_cond:
                state_cond.notify_all()
            feeder.join()
            # Close the sockets before waiting for the workers: a worker blocked in a
            # stalled response read only wakes when its socket is shut down (Neon 2).
            connections.close()
    finally:
        dl_pool.shutdown(wait=True)

    if unpack_error is not None:
        if (
            isinstance(unpack_error, Cancelled | FutureCancelled)
            and not (cancel is not None and cancel.cancelled)
            and first_dl_error
        ):
            raise first_dl_error[0]
        raise unpack_error
    if first_dl_error:
        raise first_dl_error[0]
    if cancel is not None:
        cancel.raise_if_cancelled()
    return downloaded


def _say_unpacking(archive: Archive, emit: Emitter | None) -> None:
    """Tell the user a downloaded pack is being unpacked: with the bar full and no word, the
    tens of seconds a large pack takes read as a finished install."""
    name = PurePosixPath(archive.source.path).name or archive.name
    emitter(emit)(Event("step", f"Unpacking {name}…"))


_UNWRAPS = frozenset({("zip",), ("zip", "zip"), ("7z",), ("slices",), ("hdiff",)})


def _is_https_or_loopback(url: Any) -> bool:
    """Production Sophon URLs must be https; the test server is loopback http."""
    if not isinstance(url, str):
        return False
    if url.startswith("https://"):
        return True
    return url.startswith(("http://127.0.0.1:", "http://localhost:"))


_RANGE_PIECE = re.compile(r"(?P<part>.+\.part)(?:-\d+(?:\.done)?|\.ranges\.json)")


def _range_piece_part(name: str) -> str | None:
    """The ``.part`` a range-download piece belongs to (``x.part-3``, ``x.part-3.done``,
    ``x.part.ranges.json``), or None for any other name (#1833)."""
    match = _RANGE_PIECE.fullmatch(name)
    return match["part"] if match else None


def _remove_stale_parts(root: Path, keep: set[str], managed: set[str]) -> None:
    """Best-effort removal of abandoned ``*.part`` files Nadare itself staged in ``root``,
    and the pieces of a range download of one (#1833).

    Only these go: anything inside the download cache, a ``*.nadare-new.part`` or a packed
    file's compressed copy (``*.nadare-gz`` and its ``.part``; names only Nadare uses), and the
    ``.part`` of a file the plan manages. Any other ``*.part`` may be an archive member or the
    user's own file, so it stays.
    """
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name.endswith(PACKED_SUFFIX):
                with contextlib.suppress(OSError):
                    (Path(dirpath) / name).unlink()
                continue
            part_name = _range_piece_part(name) or name
            if not part_name.endswith(".part"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            part_rel = (Path(dirpath) / part_name).relative_to(root).as_posix()
            if rel in keep or part_rel in keep:
                continue
            staged = part_rel[: -len(".part")]
            ours = (
                rel.startswith(DL_DIR + "/")
                or staged.endswith((".nadare-new", PACKED_SUFFIX))
                or staged in managed
            )
            if not ours:
                continue
            try:
                path.unlink()
            except OSError:
                pass


def _check_cache_path_clash(first: str, second: str) -> None:
    """Refuse two declared archive cache paths that would share files in ``.nadare-dl``.

    :func:`download` derives ``<path>.part`` and range pieces ``<path>.part-N`` /
    ``<path>.part-N.done`` / ``<path>.part.ranges.json`` from a cache path; a replacement
    download stages ``<path>.nadare-new`` and a packed download stages ``<path>.nadare-gz``,
    each with the same ``.part`` derivatives. A declared path equal to any of another pack's
    derived names would share the transfer with it (SEC F1, Neon 1, #1854 item 5). A path that
    needs another pack's file or derived file as a folder cannot coexist with it either.
    """
    if first == second:
        raise DirectError(f"duplicate archive cache path in plan: {first!r}")
    for path, other in ((first, second), (second, first)):
        if path in (other + ".nadare-new", other + PACKED_SUFFIX):
            raise DirectError(
                f"archive cache paths in the plan clash: {path!r} is the partial or "
                f"temporary file of {other!r}"
            )
        for base in (other, other + ".nadare-new", other + PACKED_SUFFIX):
            if path == base + ".part":
                raise DirectError(
                    f"archive cache paths in the plan clash: {path!r} is the partial or "
                    f"temporary file of {other!r}"
                )
            if path.startswith((base + ".part-", base + ".part.")):
                raise DirectError(
                    f"archive cache paths in the plan clash: {path!r} is a piece of the "
                    f"download of {other!r}"
                )
            if path.startswith((base + "/", base + ".part/")):
                raise DirectError(
                    f"archive cache paths in the plan clash: {other!r} needs a file where "
                    f"{path!r} needs a folder"
                )


def _validate_plan(plan: Plan) -> None:
    seen: set[str] = set()
    for rf in plan.files:
        normalised = safe_relative_path(rf.path)
        if normalised in seen:
            raise DirectError(f"duplicate path in plan: {normalised!r}")
        seen.add(normalised)
        if rf.packed is not None and rf.packed.codec not in PACKED_CODECS:
            raise DirectError(f"unsupported packing for {rf.path}: {rf.packed.codec!r}")
    seen_archive_paths: set[str] = set()
    declared_cache_paths: list[str] = []
    for archive in plan.archives:
        if archive.unwrap not in _UNWRAPS:
            raise DirectError(f"unsupported archive unwrap for {archive.name}: {archive.unwrap!r}")
        safe_relative_path(archive.name)
        safe_relative_path(archive.source.path)
        for item in archive.parts or (archive.source,):
            normalised = safe_relative_path(item.path)
            if normalised in seen_archive_paths:
                raise DirectError(f"duplicate archive cache path in plan: {normalised!r}")
            for other in declared_cache_paths:
                _check_cache_path_clash(normalised, other)
            first = normalised.split("/", 1)[0]
            if first == "stream" or first.startswith(_STAGING_PREFIXES):
                raise DirectError(
                    f"archive cache path {normalised!r} uses a name Nadare keeps for its own "
                    "downloads"
                )
            seen_archive_paths.add(normalised)
            declared_cache_paths.append(normalised)
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
        if archive.strip_prefix:
            if archive.unwrap not in (("zip",), ("zip", "zip")):
                raise DirectError(
                    f"strip_prefix is only supported for zip archives: {archive.name!r}"
                )
            safe_relative_path(archive.strip_prefix.rstrip("/"))
    if plan.redirect_allowed is not None:
        if plan.sophon:
            raise DirectError("a redirect allow-list cannot be applied to Sophon packages")
        for archive in plan.archives:
            if archive.stream:
                raise DirectError(
                    f"a redirect allow-list cannot be applied to streamed archive {archive.name!r}"
                )
    for pkg in plan.sophon:
        safe_relative_path(pkg.name)
        for label, url in (("manifest", pkg.manifest_url), ("chunk", pkg.chunk_url)):
            if not _is_https_or_loopback(url):
                raise DirectError(f"sophon {label} URL is not https: {url!r}")


def require_https(url: Any, what: str = "URL") -> str:
    """``url`` when it is an https URL with a host; else :class:`DirectError` (#1455 R4-7).

    The one check every publisher source uses for the URLs it is handed."""
    if isinstance(url, str) and url:
        parts = urlsplit(url)
        if parts.scheme.lower() == "https" and parts.netloc:
            return url
    raise DirectError(f"{what} is not an https URL: {url!r}")


def safe_relative_path(path: str) -> str:
    """Return ``path`` normalised, or raise :class:`DirectError` for an absolute,
    empty, ``..``-containing or backslash-escaping path."""
    try:
        return paths.check_relative(path, paths.DIRECT_PATHS)
    except paths.PathProblem as exc:
        if exc.code == "not-string" or exc.code == "empty":
            message = "path must not be empty"
        elif exc.code == "backslash":
            message = f"path must not contain a backslash: {path!r}"
        elif exc.code == "absolute":
            message = f"path must be relative: {path!r}"
        elif exc.code == "dotdot":
            message = f"path must not contain '..': {path!r}"
        elif exc.code == "empty-normalised":
            message = f"path is empty after normalisation: {path!r}"
        elif exc.code == "nul":
            message = f"path must not contain a NUL: {path!r}"
        elif exc.code == "nadare-owned":
            message = f"path is inside a Nadare-owned folder: {path!r}"
        else:
            message = f"unsafe path: {path!r}"
        raise DirectError(message) from None


def plan_missing(
    plan: Plan,
    dest: Path | str,
    *,
    verify: bool = True,
    check_workers: int | None = None,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[RemoteFile], list[Archive]]:
    """What :func:`sync` would still fetch: files that are absent or whose size/hash
    differ, and archives with no ``.done`` record for this plan's version.

    ``verify=False`` compares sizes only: right after a sync that already hashed every file.
    File checks run in bounded batches; the returned missing-file list keeps plan order.
    ``progress(checked, total)`` is called now and then while files are checked, and once
    at the end.
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

    total = len(plan.files)
    last_report = float("-inf")

    def report(checked: int) -> None:
        nonlocal last_report
        if progress is None:
            return
        now = time.monotonic()
        if checked >= total or now - last_report >= _CHECK_PROGRESS_INTERVAL:
            last_report = now
            progress(checked, total)

    files: list[RemoteFile] = []
    if workers == 1:
        for index, rf in enumerate(plan.files, 1):
            if needs_download(rf):
                files.append(rf)
            report(index)
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
                report(offset + len(batch))
    archives = [
        archive
        for archive in plan.archives
        if not _archive_done(plan, archive, root, hashes=verify)
    ]
    return files, archives


def _archive_record_for_game(plan: Plan, root: Path) -> bool:
    """Whether an earlier archive install of this game left a ``.done`` record."""
    state = root / STATE_DIR
    try:
        candidates = sorted(state.rglob("*.done"))
    except OSError:
        return False
    for record_path in candidates:
        record = _read_done(record_path)
        if record is None:
            continue
        game = record.get("game")
        if game == plan.game_tag:
            return True
        if game is None and record_path.relative_to(state).as_posix().startswith(
            f"{plan.game_tag}-"
        ):
            return True
    return False


def sophon_missing(plan: Plan, dest: Path | str) -> list[sophon.SophonPackage]:
    """Sophon packages with no ``.done`` marker for their manifest id.

    An install an earlier release made from an archive has an archive record but no Sophon
    markers. Its files are verified chunk by chunk by the next sync, not reported missing. That
    excuse ends once Sophon has run for the game (any marker exists): the archive record stays on
    disk for good and must not hide a package that is really missing later.
    """
    root = Path(dest)
    missing = [pkg for pkg in plan.sophon if not sophon.is_done(pkg, root)]
    never_synced = len(missing) == len(plan.sophon) and not any(
        (root / STATE_DIR).glob("sophon-*.done")
    )
    if missing and never_synced and _archive_record_for_game(plan, root):
        return []
    return missing


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


_MD5_HEX = frozenset("0123456789abcdefABCDEF")


def cached_file(cache: Path | str, rf: RemoteFile) -> Path:
    """Where a predownload keeps ``rf``: ``cache/files/<md5>``."""
    md5 = str(rf.md5 or "")
    if len(md5) != 32 or any(char not in _MD5_HEX for char in md5):
        raise DirectError(f"{rf.path} has no md5 to cache it by")
    return Path(cache) / "files" / md5.lower()


def _cached_regular(cache: Path | str, rf: RemoteFile) -> Path | None:
    """The cache path when it is a regular file, never following a planted link."""
    try:
        path = cached_file(cache, rf)
        info = os.lstat(path)
    except (OSError, DirectError):
        return None
    return path if stat.S_ISREG(info.st_mode) else None


def _take_cached_file(cached: Path, target: Path) -> None:
    """Move a verified cached file into place, copying when the cache is on another filesystem."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(cached, target)
    except OSError:
        temporary = target.with_name(target.name + ".nadare-cache")
        try:
            shutil.copyfile(cached, temporary)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def predownload(
    plan: Plan,
    dest: Path | str,
    cache: Path | str,
    *,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    workers: int = 4,
    files: Sequence[RemoteFile] | None = None,
) -> dict[str, int]:
    """Fetch what ``plan`` needs ahead of its release into ``cache``.

    One cache folder holds every Sophon package of the plan. A file plan keeps each file at
    ``cache/files/<md5>``. The installed game is only read; the update that follows takes
    verified files from this cache before the network.
    """
    cache_dir = Path(cache)
    if plan.sophon:
        root = Path(dest).expanduser()
        downloaded = 0
        for pkg in plan.sophon:
            try:
                downloaded += sophon.predownload_package(
                    pkg, root, cache_dir, emit=emit, cancel=cancel, workers=workers
                )
            except sophon.SophonError as exc:
                raise DirectError(str(exc)) from exc
        return {"bytes": downloaded, "packages": len(plan.sophon)}

    if files is None and not plan.files and not plan.archives:
        raise DirectError("this game's download cannot be fetched ahead")
    selected = list(files) if files is not None else plan_missing(plan, dest)[0]
    for rf in selected:  # refuse the whole plan before writing anything
        cached_file(cache_dir, rf)
    (cache_dir / "files").mkdir(parents=True, exist_ok=True)
    stop = Cancel()
    combined = _CombinedCancel(cancel, stop)
    overall = _OverallProgress(selected, emit)

    def fetch(rf: RemoteFile) -> int:
        combined.raise_if_cancelled()
        target = cached_file(cache_dir, rf)
        cached = _cached_regular(cache_dir, rf)
        if cached is not None and _verify_remote(cached, rf, cancel=combined):
            overall.finished(rf)
            return 0
        _download_remote(
            rf,
            target,
            emit=overall.sink(rf),
            cancel=combined,
            redirect_allowed=plan.redirect_allowed,
        )
        overall.finished(rf)
        return int(rf.size or target.stat().st_size)

    downloaded = 0
    first_error: BaseException | None = None
    max_workers = max(1, min(workers, MAX_WORKERS))
    if selected:
        try:
            with ThreadPoolExecutor(
                initializer=download_module.inherit_transfer,
                initargs=(download_module.current_transfer(),),
                max_workers=max_workers,
            ) as pool:
                futures = [pool.submit(fetch, rf) for rf in selected]
                for future in as_completed(futures):
                    try:
                        downloaded += future.result()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                            stop.cancel()
                            for other in futures:
                                other.cancel()
        finally:
            if first_error is None:
                overall.flush()
    if first_error is not None:
        raise first_error
    (cache_dir / "ready.json").write_text(
        json.dumps({"version": plan.version, "files": len(selected)}), encoding="utf-8"
    )
    return {"bytes": downloaded, "files": len(selected)}


def predownload_ready(plan: Plan, cache: Path | str) -> bool:
    """True when ``cache`` holds everything ``plan`` recorded as ready."""
    cache_dir = Path(cache)
    if plan.sophon:
        return all(sophon.predownload_ready(pkg, cache_dir) for pkg in plan.sophon)
    path = cache_dir / "ready.json"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("version") == plan.version



def worker_cap(plan: Plan, value: Any = None) -> int:
    """A profile limits the plan default; historically it replaced it."""
    default = plan_workers(plan)
    try:
        return min(default, max(1, min(32, int(value)))) if value not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        return default


def plan_workers(plan: Plan) -> int:
    """The default number of parallel downloads for ``plan``.

    A source whose CDN caps each connection can ask for more with ``extra["workers"]`` (Snowbreak,
    #1611); a bad hint is ignored, a big one capped at :data:`MAX_WORKERS`.
    """
    if plan.sophon:
        return _SOPHON_WORKERS
    hint = plan.extra.get("workers") if isinstance(plan.extra, Mapping) else None
    if isinstance(hint, int) and not isinstance(hint, bool) and hint > 0:
        return min(hint, MAX_WORKERS)
    return default_workers(len(plan.files))


def _install_lock(install_dir: Path) -> Iterator[None]:
    """Refuse to run two direct syncs into one install tree (they share ``.part`` files)."""
    return installlock.hold(
        install_dir,
        DirectError,
        f"another install or update is already running for {install_dir}",
        busy=InstallBusy,
    )


def install_lock(install_dir: Path | str) -> Iterator[None]:
    """The lock :func:`sync` takes, for a caller that must hold it over more than the sync
    (CZN: the plan check, the sync and its version marker, #1557). Such a caller passes
    ``lock=False`` to :func:`sync`. :class:`InstallBusy` when another sync holds it."""
    return _install_lock(Path(install_dir).expanduser())


def _read_done(record_path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _record_host(url: object) -> str | None:
    """The lowercased host of a URL, or None when it is missing or cannot be parsed."""
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _plan_archive_hosts(plan: Plan) -> set[str]:
    hosts: set[str] = set()
    for archive in plan.archives:
        for remote in (archive.source, *archive.parts):
            host = _record_host(remote.url)
            if host:
                hosts.add(host)
    return hosts


def _superseded_records(plan: Plan, root: Path) -> list[Path]:
    """Records of earlier versions of a slotted archive of ``plan`` (renamed every version).

    A record with the same slot and another name is one. A record whose ``game`` names another
    game is never one. When ``plan`` lists the whole install (``prune_files``), every other
    record of its game is one, slotted or not, even when the new version has no archive at all
    (NTE/ToF: a version may ship a Pak's files loose, #1824). Otherwise a record without a slot
    (written before its archive had one) counts only when ``plan`` has a single archive: then
    every other archive record in that game's folder can only be an older version of it, while
    beside several archives it may be DLC. A record written before records named their game
    has no ``game``; it also needs a URL from a host the plan downloads from.
    """
    slots = {archive.slot for archive in plan.archives if archive.slot and not archive.additive}
    if not slots and not plan.prune_files:
        return []
    current = {safe_relative_path(archive.name) + ".done" for archive in plan.archives}
    state = root / STATE_DIR
    try:
        candidates = sorted(state.rglob("*.done"))
    except OSError:
        return []
    legacy_hosts = _plan_archive_hosts(plan)
    old: list[Path] = []
    for record_path in candidates:
        if record_path.relative_to(state).as_posix() in current:
            continue
        record = _read_done(record_path)
        if record is None:
            continue
        game = record.get("game")
        if game is not None and game != plan.game_tag:
            continue
        slot = record.get("slot")
        if not (
            plan.prune_files
            or (isinstance(slot, str) and slot in slots)
            or (slot is None and len(plan.archives) == 1)
        ):
            continue
        if game is None and _record_host(record.get("url")) not in legacy_hosts:
            continue
        old.append(record_path)
    return old


def _record_order(record_path: Path, record: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """When a receipt was applied: its recorded sequence, else base before additive.

    Older receipts have no ``sequence``: order them base first, then by modification time
    (``lstat``: never follow a link planted at a receipt path) and finally by name.
    """
    sequence = record.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return (1, sequence, 0, record_path.name)
    try:
        modified = record_path.lstat().st_mtime_ns
    except OSError:
        modified = 0
    return (0, 1 if record.get("additive") is True else 0, modified, record_path.name)


def _recorded_members(plan: Plan, root: Path) -> dict[str, dict[str, Any]]:
    """What the archives of ``plan`` owned before this sync: ``{path: {"size", "crc32"}}``.

    Earlier versions of a renamed archive count too (``_superseded_records``). Only a complete
    inventory counts (a truncated or partial one cannot say what is gone)."""
    owned: dict[str, dict[str, Any]] = {}
    paths = [root / STATE_DIR / f"{safe_relative_path(a.name)}.done" for a in plan.archives]
    records = []
    for record_path in [*_superseded_records(plan, root), *paths]:
        record = _read_done(record_path)
        if record is not None:
            records.append((_record_order(record_path, record), record))
    # Deltas overwrite base files: the last applied receipt owns their current checksums.
    for _, record in sorted(records, key=lambda item: item[0]):
        if record.get("members_truncated"):
            continue
        members, sizes, crcs = (
            record.get("members"),
            record.get("member_sizes"),
            record.get("member_crc32"),
        )
        if not (isinstance(members, list) and isinstance(sizes, dict) and isinstance(crcs, dict)):
            continue
        for member in members:
            if isinstance(member, str) and member in sizes and member in crcs:
                owned[member] = {"size": sizes[member], "crc32": crcs[member]}
    return owned


_FILE_LEDGER_LIMIT = 64 << 20


def _file_ledger_path(plan: Plan, root: Path) -> Path:
    name = f"files-{plan.game_tag}.json"
    if "/" in name or "\\" in name or name.startswith("."):
        raise DirectError(f"unsafe game tag for a file ledger: {plan.game_tag!r}")
    return root / STATE_DIR / name


def _read_file_ledger(plan: Plan, root: Path) -> dict[str, dict[str, Any]]:
    """The files the last successful sync of ``plan``'s game installed: ``{path: {size, md5}}``.

    Empty when absent, a link, too big, damaged or another game's: nothing is then removed."""
    try:
        fd = os.open(_file_ledger_path(plan, root), os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, DirectError):
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            raw = handle.read(_FILE_LEDGER_LIMIT + 1)
        if len(raw) > _FILE_LEDGER_LIMIT:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("game") != plan.game_tag:
        return {}
    files = payload.get("files")
    return _usable_ledger_entries(files) if isinstance(files, dict) else {}


def _usable_ledger_entries(files: Mapping[Any, Any]) -> dict[str, dict[str, Any]]:
    """The entries of a ledger (or a caller's seed) that name a safe path, a size and an md5."""
    usable: dict[str, dict[str, Any]] = {}
    for path, entry in files.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            continue
        size, md5 = entry.get("size"), entry.get("md5")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            continue
        if not isinstance(md5, str) or len(md5) != 32:
            continue
        try:
            path = safe_relative_path(path)
        except DirectError:
            continue
        if path.split("/", 1)[0] in (STATE_DIR, DL_DIR):
            continue
        usable[path] = {"size": size, "md5": md5.lower()}
    return usable


_CRC32_TEXT = re.compile(r"[0-9A-Fa-f]{1,8}")


def _pending_prune_path(plan: Plan, root: Path) -> Path:
    name = f"prune-pending-{plan.game_tag}.json"
    if "/" in name or "\\" in name:
        raise DirectError(f"unsafe game tag for a prune list: {plan.game_tag!r}")
    return root / STATE_DIR / name


def _read_pending_prune(plan: Plan, root: Path) -> dict[str, dict[str, Any]]:
    """What the archives and ledger owned when a sync of this game that never finished began.

    Fixed-name archives (R1999 package-00..19) replace their record as each one finishes, so
    after a stop only this still says what the older version owned (#1829). Entries keep their
    own check (``crc32`` or ``md5`` and ``size``); anything unreadable counts as nothing.
    """
    try:
        fd = os.open(_pending_prune_path(plan, root), os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, DirectError):
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            raw = handle.read(_FILE_LEDGER_LIMIT + 1)
        if len(raw) > _FILE_LEDGER_LIMIT:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("game") != plan.game_tag:
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return {}
    usable = _usable_ledger_entries(
        {p: e for p, e in files.items() if isinstance(e, dict) and "md5" in e}
    )
    for path, entry in files.items():
        if not isinstance(path, str) or not isinstance(entry, dict) or "md5" in entry:
            continue
        size, crc = entry.get("size"), entry.get("crc32")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            continue
        if not (isinstance(crc, str) and _CRC32_TEXT.fullmatch(crc)):
            continue  # records keep CRC-32 as hex text
        try:
            safe = safe_relative_path(path)
        except DirectError:
            continue
        if safe.split("/", 1)[0] in (STATE_DIR, DL_DIR):
            continue
        usable[safe] = {"size": size, "crc32": crc}
    return usable


def _write_pending_prune(plan: Plan, root: Path, owned: Mapping[str, Any]) -> None:
    """Keep ``owned`` until a sync of this game completes (#1829); nothing to keep, no file."""
    if not owned:
        return
    try:
        if not root.is_dir():
            return  # a first install: there is nothing it could have dropped
        _write_record(_pending_prune_path(plan, root), {"game": plan.game_tag, "files": owned})
    except (OSError, DirectError):
        log.debug("could not keep the pending prune list", exc_info=True)


def _clear_pending_prune(plan: Plan, root: Path) -> None:
    try:
        _pending_prune_path(plan, root).unlink(missing_ok=True)
    except (OSError, DirectError):
        log.debug("could not remove the pending prune list", exc_info=True)


def _write_file_ledger(plan: Plan, root: Path) -> None:
    files = {
        safe_relative_path(rf.path): {"size": rf.size, "md5": rf.md5.lower()}
        for rf in plan.files
        if rf.md5 and rf.size is not None
    }
    try:
        _write_record(
            _file_ledger_path(plan, root),
            {"game": plan.game_tag, "version": str(plan.version), "files": files},
        )
    except (OSError, DirectError) as exc:
        log.debug("%s: could not record the installed files: %s", plan.game_tag, exc)


def _dropped_loose_files(
    plan: Plan,
    root: Path,
    keep: Iterable[str] = (),
    seed: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Ledger entries ``plan`` (and ``keep``) no longer lists; a case-only rename is no drop.

    Without a ledger, ``seed`` (the caller's own record of the previous version) stands in."""
    if not plan.prune_files:
        return {}
    current = {safe_relative_path(rf.path).lower() for rf in plan.files}
    current.update(name.lower() for name in keep)
    before = _read_file_ledger(plan, root)
    if not before and seed:
        before = _usable_ledger_entries(seed)
    return {p: e for p, e in before.items() if p.lower() not in current}


def prune_dropped_files(
    plan: Plan,
    root: Path | str,
    *,
    keep: Iterable[str] = (),
    seed: Mapping[str, Mapping[str, Any]] | None = None,
) -> int:
    """For an updater that synced only part of ``plan`` (#1825, CZN): remove the files the
    previous version installed that ``plan`` no longer lists, then record ``plan`` as installed.

    Call it only after the update succeeded, holding the install lock. ``plan`` is the whole
    version (every file it ships); ``keep`` names paths the updater never manages and so must
    never remove. The same guards as a full sync apply. Returns how many files were removed.
    """
    root = Path(root)
    plan = replace(plan, prune_files=True)
    removed = _prune_dropped_members(plan, root, _dropped_loose_files(plan, root, keep, seed))
    _write_file_ledger(plan, root)
    return removed


def _prune_dropped_members(
    plan: Plan,
    root: Path,
    before: Mapping[str, Mapping[str, Any]],
    superseded: Sequence[Path] = (),
) -> int:
    """Delete files a previous version of an archive installed that no version now owns.

    Runs only after a whole sync succeeded. A file goes only when it is a plain file (never a
    link), still the size and CRC-32 the old record gave (the user did not change it), and
    nothing owns it any more: not a loose file of ``plan``, not any ``.done`` record
    (another archive or DLC), not a Sophon ledger. Folders are never removed. ``superseded``
    records (earlier versions of an archive that was just replaced) own nothing any more; they
    are removed after the prune.
    """
    if not before:
        return 0
    owned = {safe_relative_path(rf.path) for rf in plan.files}
    owned_lower = {path.lower() for path in owned}
    state = root / STATE_DIR
    try:
        # An archive name may hold folders (its record is then in a sub-folder): the glob must
        # reach it, or that archive's own files look unowned and are deleted.
        records = sorted(state.rglob("*.done"))
    except OSError:
        return 0
    gone = {Path(p) for p in superseded}
    for record_path in records:
        if record_path in gone:
            continue
        record = _read_done(record_path)
        if record is None or record.get("members_truncated"):
            return 0  # cannot tell what is owned: remove nothing
        members = record.get("members")
        if isinstance(members, list):
            owned.update(m for m in members if isinstance(m, str))
    for ledger in state.glob("sophon-*.files"):
        try:
            owned.update(line for line in ledger.read_text(encoding="utf-8").splitlines() if line)
        except (OSError, UnicodeError):
            return 0
    try:
        own_ledger: Path | None = _file_ledger_path(plan, root)
    except DirectError:
        own_ledger = None
    for ledger in state.glob("files-*.json"):
        if ledger == own_ledger:
            continue  # this plan's previous ledger is what ``before`` came from
        try:
            payload = json.loads(ledger.read_bytes())
            listed = payload["files"]
        except (OSError, ValueError, TypeError, KeyError):
            return 0  # cannot tell what another install owns: remove nothing
        if not isinstance(listed, dict):
            return 0
        owned.update(path for path in listed if isinstance(path, str))
        owned_lower.update(path.lower() for path in listed if isinstance(path, str))
    removed = 0
    for member, old in before.items():
        if member in owned or ("md5" in old and member.lower() in owned_lower):
            continue
        try:
            path = _safe_destination_path(root, safe_relative_path(member))
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != old["size"]:
                continue
            if "md5" in old:  # a loose file's ledger entry (#1825)
                if _md5_file(path) != old["md5"]:
                    continue
            elif _crc32_file(path) != str(old["crc32"]).upper():
                continue
            path.unlink()
            removed += 1
        except (OSError, DirectError):
            continue
    for record_path in gone:
        for stale in (record_path, _member_list_path(record_path)):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    return removed


def sync(
    plan: Plan,
    dest: Path | str,
    *,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    workers: int = 4,
    check_workers: int | None = None,
    verify: bool = True,
    changed: threading.Event | None = None,
    chunk_cache: Path | None = None,
    lock: bool = True,
) -> dict[str, int]:
    """Install ``plan`` into ``dest``. Returns ``{"downloaded": n, "skipped": m}``
    counting files plus archives. Idempotent: a second run fetches nothing.

    ``verify=False`` compares sizes only where a caller just wrote every file from an archive
    (the md5 pass is Verify's job). ``changed`` is set as soon as a file in ``dest`` was replaced
    or an archive started to unpack, so a caller whose sync fails can tell an untouched install
    from a half-updated one (replacements are per file, there is no rollback). ``lock=False`` only
    for a caller that already holds :func:`install_lock` for ``dest``."""
    _validate_plan(plan)
    root = Path(dest).expanduser()
    if root.exists() and not root.is_dir():
        raise DirectError(f"install path is not a folder: {root}")
    with (
        download_module.transfer.measure(workers, emit),
        _install_lock(root) if lock else contextlib.nullcontext(),
    ):
        measurement = download_module.current_transfer()
        concurrent_files = max(len(plan.files), _peak_archive_concurrency(plan.archives))
        measurement.range_workers = max(
            1, min(workers, MAX_WORKERS) // max(1, min(concurrent_files, workers))
        )
        return _sync(
            plan,
            root,
            emit=emit,
            cancel=cancel,
            workers=workers,
            check_workers=check_workers,
            verify=verify,
            changed=changed,
            chunk_cache=chunk_cache,
        )


def _sync(
    plan: Plan,
    root: Path,
    *,
    emit: Emitter | None,
    cancel: Cancel | None,
    workers: int,
    check_workers: int | None = None,
    verify: bool = True,
    changed: threading.Event | None = None,
    chunk_cache: Path | None = None,
) -> dict[str, int]:
    say = emitter(emit)
    if cancel is not None:
        cancel.raise_if_cancelled()
    _sweep_stale_staging(root)
    superseded = _superseded_records(plan, root)
    # Loose ledger first: an archive record that also lists a path has the final say. What a
    # stopped earlier sync saw comes first of all: since then a finished archive's record may
    # already be the new version's (#1829).
    owned_before = {
        **_read_pending_prune(plan, root),
        **_dropped_loose_files(plan, root),
        **_recorded_members(plan, root),
    }
    _write_pending_prune(plan, root, owned_before)

    def checking(done: int, total: int) -> None:
        # A step, not "progress": the bar and its consumers mean download progress.
        say(Event("step", f"Checking the files already on disk: {done:,} of {total:,}"))

    missing_files, missing_archives = plan_missing(
        plan, root, verify=verify, check_workers=check_workers, cancel=cancel, progress=checking
    )
    downloaded = 0
    fetched_bytes = 0
    stop = Cancel()
    combined = _CombinedCancel(cancel, stop)

    if missing_files:
        max_workers = max(1, min(workers, MAX_WORKERS))

        # One keep-alive connection (and one DNS lookup) per worker thread and host, not per
        # file: tens of thousands of lookups a minute trip a resolver's rate limit (#767).
        connections = download_module.HttpClientPool()

        overall = _OverallProgress(missing_files, emit)

        def fetch(rf: RemoteFile) -> None:
            combined.raise_if_cancelled()
            target = _safe_destination_path(root, rf.path, directory=False)
            if chunk_cache is not None:
                cached = _cached_regular(chunk_cache, rf)
                if cached is not None and _verify_remote(cached, rf, cancel=combined):
                    _take_cached_file(cached, target)
                    if changed is not None:
                        changed.set()
                    overall.from_cache(rf)
                    return
            _download_remote(
                rf,
                target,
                emit=overall.sink(rf),
                cancel=combined,
                pool=connections,
                redirect_allowed=plan.redirect_allowed,
            )
            if changed is not None:
                changed.set()
            overall.finished(rf)

        first_error: BaseException | None = None
        try:
            with ThreadPoolExecutor(
                initializer=download_module.inherit_transfer,
                initargs=(download_module.current_transfer(),),
                max_workers=max_workers,
            ) as pool:
                futures = [pool.submit(fetch, rf) for rf in missing_files]
                for future in as_completed(futures):
                    try:
                        future.result()
                        downloaded += 1
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                            stop.cancel()
                            # Once only: every queued file then ends here as cancelled, and
                            # walking all futures for each of them is quadratic (#562).
                            for other in futures:
                                other.cancel()
        finally:
            connections.close()
        if first_error is not None:
            raise first_error
        overall.flush()
        fetched_bytes += overall.fetched

    if missing_files and missing_archives:
        # The loose files may have been most of the bytes (NTE: 89 GB, then 7 packs); say that
        # more follows, so the pack downloads do not read as a surprise (#1612).
        size = sum(a.source.size or 0 for a in missing_archives)
        packs = f"{len(missing_archives)} pack{'s' if len(missing_archives) != 1 else ''}"
        total = f" ({diskspace.human_size(size)})" if size else ""
        say(Event("step", f"Second pass: {packs}{total}"))
    if missing_archives:
        # Count what really crossed the wire for the packs: a verified archive reused from
        # .nadare-dl after a stop fetches nothing (#1830, sim-pack F1). The transfer meter
        # already sums every body byte read, so snapshot it around the pack phase.
        measurement = download_module.current_transfer()
        fetched_before = getattr(measurement, "bytes", 0)
        downloaded += _sync_archives(
            plan,
            missing_archives,
            root,
            emit=emit,
            cancel=cancel,
            workers=workers,
            changed=changed,
        )
        fetched_bytes += max(0, getattr(measurement, "bytes", 0) - fetched_before)

    for pkg in plan.sophon:
        if cancel is not None:
            cancel.raise_if_cancelled()
        say(Event("step", f"installing {pkg.name}"))
        try:
            sophon.sync_package(
                pkg,
                root,
                emit=emit,
                cancel=cancel,
                workers=workers,
                chunk_cache=chunk_cache,
                changed=changed,
            )
        except sophon.SophonError as exc:
            raise DirectError(str(exc)) from exc
        downloaded += 1

    keep_parts = {safe_relative_path(rf.path) for rf in plan.files}
    can_prune_parts = True
    for pkg in plan.sophon:
        # Sophon owns its receipt (#1464); an unsure answer keeps every part.
        owned = sophon.owned_files(pkg, root)
        if not owned.certain:
            can_prune_parts = False
            break
        keep_parts.update(owned.paths)
    if can_prune_parts:
        _remove_stale_parts(root, keep_parts, {safe_relative_path(rf.path) for rf in plan.files})
    try:
        (root / DL_DIR).rmdir()  # only succeeds when nothing is left in it
    except OSError:
        pass

    _prune_dropped_members(plan, root, owned_before, superseded)
    if plan.prune_files:
        _write_file_ledger(plan, root)
    _clear_pending_prune(plan, root)
    skipped = (
        len(plan.files) + len(plan.archives) + len(plan.sophon) - downloaded
    )
    return SyncSummary(downloaded=downloaded, skipped=skipped, fetched_bytes=fetched_bytes)
