# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A read-only, seekable file over HTTP(S) objects, fetched with Range requests.

``zipfile`` and :mod:`nadare.core.sevenz` need a seekable file. With this they can read
an archive that lives on a server (or a split archive made of several objects) without
downloading it first: the zip's central directory is fetched from the end, and each
member only when it is wanted. Reads are served from aligned blocks, so a member
read in small steps costs a few large requests, not many small ones.

One reader is for one thread; give each worker its own.
"""

from __future__ import annotations

import bisect
import hashlib
import http.client
import io
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import download
from .events import Cancel

DEFAULT_BLOCK = 4 << 20


class RangeError(RuntimeError):
    """The server cannot be read by range, or kept failing."""


class BlockMismatch(RuntimeError):
    """A fetched block does not match the published sha256 list.

    ``part_index`` and ``block_index`` are zero-based; ``url`` is the part's URL.
    """

    def __init__(self, part_index: int, block_index: int, url: str) -> None:
        self.part_index = part_index
        self.block_index = block_index
        self.url = url
        super().__init__(f"block {block_index} of {url} does not match the published checksum list")


@dataclass(frozen=True)
class RangePart:
    """One object; ``size`` is looked up with HEAD when it is not known."""

    url: str
    size: int | None = None


class RangeReader(io.RawIOBase):
    """Seekable file over ``parts`` (concatenated in order).

    ``block_size`` is the unit of every request; ``cache_blocks`` recent blocks are
    kept. When ``readahead`` exceeds ``block_size``, a sequential miss fetches up to
    ``readahead`` bytes (ramping up from one block) in one request and caches the
    blocks it covers. ``requests`` and ``bytes_fetched`` count the ranged GETs made.
    Network errors and 5xx answers are retried ``retries`` times with ``backoff``
    seconds (doubling); other 4xx answers fail at once. A server that answers a Range
    request with 200 raises :class:`RangeError`. ``cancel`` is checked before each
    request.
    """

    def __init__(
        self,
        parts: Sequence[RangePart | tuple[str, int | None] | str],
        *,
        headers: Mapping[str, str] | None = None,
        block_size: int = DEFAULT_BLOCK,
        cache_blocks: int = 4,
        readahead: int = 0,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: int = 60,
        cancel: Cancel | None = None,
        block_hashes: Sequence[Sequence[bytes]] | None = None,
    ) -> None:
        if block_size < 1:
            raise ValueError("block_size must be at least 1")
        self._headers = dict(headers or {})
        self._block_size = block_size
        self._readahead = readahead
        self._blocks_ahead = max(1, readahead // block_size) if readahead > block_size else 1
        # Readahead needs room for the block it was asked for plus the ones it fetched.
        limit = (
            max(cache_blocks, self._blocks_ahead + 1) if readahead > block_size else cache_blocks
        )
        self._cache_limit = limit
        self._retries = retries
        self._backoff = backoff
        self._timeout = timeout
        self._cancel = cancel
        self._block_hashes = (
            [tuple(blocks) for blocks in block_hashes] if block_hashes is not None else None
        )
        self._pool = download.HttpClientPool(timeout=timeout)
        self._cache: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._last: tuple[int, int] | None = None
        #: Blocks the next sequential miss will fetch; doubles per sequential miss.
        self._ahead = 1
        self._pos = 0
        self.requests = 0
        self.bytes_fetched = 0

        parts = list(parts)
        resolved: list[RangePart] = []
        for part in parts:
            if isinstance(part, str):
                part = RangePart(part)
            elif isinstance(part, tuple):
                part = RangePart(*part)
            size = part.size
            if size is None:
                size = download.remote_size(part.url, headers=self._headers, timeout=timeout)
            if not size or size < 0:
                raise RangeError(f"cannot determine size of {part.url}")
            resolved.append(RangePart(part.url, size))

        if self._block_hashes is not None:
            if len(self._block_hashes) != len(resolved):
                raise ValueError("block_hashes must have one list per part")
            for blocks, part in zip(self._block_hashes, resolved, strict=True):
                expected = ((part.size or 0) + block_size - 1) // block_size
                if len(blocks) != expected:
                    raise ValueError(
                        f"block_hashes for {part.url} has {len(blocks)} blocks, expected {expected}"
                    )

        self._parts = resolved
        self._starts: list[int] = []
        self._ends: list[int] = []
        total = 0
        for part in resolved:
            self._starts.append(total)
            total += part.size  # type: ignore[operator]
            self._ends.append(total)
        self.size = total

    #: Total size of all parts.
    size: int
    #: Number of ranged GETs made and bytes received by them.
    requests: int
    bytes_fetched: int

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def _locate(self, pos: int) -> tuple[int, int]:
        index = bisect.bisect_right(self._ends, pos)
        return index, pos - self._starts[index]

    def _fetch_block(self, part_index: int, start: int, end: int) -> bytes:
        part = self._parts[part_index]
        length = end - start + 1
        headers = {**self._headers, "Range": f"bytes={start}-{end}"}
        with self._pool.open(part.url, headers=headers, timeout=self._timeout) as resp:
            status = resp.status
            if status == 200:
                raise RangeError(
                    f"server does not support range requests for {part.url}"
                )
            if status != 206:
                raise download.DownloadError(f"HTTP {status} for {part.url}")
            content_range = resp.headers.get("Content-Range", "")
            try:
                first = int(content_range.split()[1].split("-", 1)[0])
            except (IndexError, ValueError) as exc:
                raise RangeError(
                    f"invalid Content-Range from {part.url}: {content_range!r}"
                ) from exc
            if first != start:
                raise RangeError(
                    f"Content-Range from {part.url} starts at {first}, wanted {start}"
                )
            data = resp.read(length)
        if len(data) != length:
            raise _ShortRead(f"short body from {part.url}: {len(data)} of {length} bytes")
        return data

    def _block(self, part_index: int, block_index: int) -> bytes:
        key = (part_index, block_index)
        previous = self._last
        self._last = key
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        part_size = self._parts[part_index].size or 0
        count = 1
        if self._readahead > self._block_size:
            if previous == (part_index, block_index - 1):
                # Ramp up like an OS read-ahead, so a small member that happens to
                # span two blocks does not pull in megabytes.
                count = self._ahead
                self._ahead = min(self._ahead * 2, self._blocks_ahead)
            else:
                self._ahead = 1
        start = block_index * self._block_size
        end = min(start + count * self._block_size, part_size) - 1
        error: BaseException | None = None
        attempt = 0
        refetched = False
        while True:
            if self._cancel is not None:
                self._cancel.raise_if_cancelled()
            try:
                data = self._fetch_block(part_index, start, end)
            except RangeError:
                raise
            except download.DownloadError as exc:
                if "HTTP 4" in str(exc):
                    raise RangeError(str(exc)) from exc
                error = exc
            except (OSError, http.client.HTTPException, _ShortRead) as exc:
                error = exc
            else:
                self.requests += 1
                self.bytes_fetched += len(data)
                if self._block_hashes is not None:
                    bad = self._mismatched_block(part_index, block_index, data)
                    if bad is not None:
                        if refetched:
                            raise BlockMismatch(part_index, bad, self._parts[part_index].url)
                        # One corrupt answer may be a bad edge: ask again before refusing.
                        refetched = True
                        continue
                wanted = data
                if self._cache_limit > 0:
                    for offset in range(0, len(data), self._block_size):
                        chunk_key = (part_index, block_index + offset // self._block_size)
                        self._cache[chunk_key] = data[offset : offset + self._block_size]
                        self._cache.move_to_end(chunk_key)
                    while len(self._cache) > self._cache_limit:
                        self._cache.popitem(last=False)
                    wanted = self._cache[key]
                return wanted
            if attempt < self._retries and self._backoff:
                time.sleep(self._backoff * 2**attempt)
            attempt += 1
            if attempt > self._retries:
                raise RangeError(
                    f"failed to fetch bytes {start}-{end} of {self._parts[part_index].url}"
                ) from error

    def _mismatched_block(self, part_index: int, first_block: int, data: bytes) -> int | None:
        """The first block in ``data`` whose sha256 is not in the table, if any."""
        assert self._block_hashes is not None
        expected = self._block_hashes[part_index]
        for offset in range(0, len(data), self._block_size):
            block_index = first_block + offset // self._block_size
            chunk = data[offset : offset + self._block_size]
            if hashlib.sha256(chunk).digest() != expected[block_index]:
                return block_index
        return None

    def readinto(self, buffer) -> int:  # type: ignore[no-untyped-def]
        self._check_open()
        view = memoryview(buffer).cast("B")
        wanted = len(view)
        if wanted == 0 or self._pos >= self.size:
            return 0
        written = 0
        while written < wanted and self._pos < self.size:
            part_index, local = self._locate(self._pos)
            block_index = local // self._block_size
            data = self._block(part_index, block_index)
            offset = local % self._block_size
            part_left = (self._parts[part_index].size or 0) - local
            take = min(len(data) - offset, wanted - written, part_left)
            view[written : written + take] = data[offset : offset + take]
            written += take
            self._pos += take
        return written

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._check_open()
        if whence == io.SEEK_SET:
            base = 0
        elif whence == io.SEEK_CUR:
            base = self._pos
        elif whence == io.SEEK_END:
            base = self.size
        else:
            raise ValueError(f"invalid whence: {whence}")
        position = base + offset
        if position < 0:
            raise ValueError(f"negative seek position {position}")
        self._pos = position
        return position

    def tell(self) -> int:
        self._check_open()
        return self._pos

    def close(self) -> None:
        if not self.closed:
            self._cache.clear()
            self._pool.close()
            super().close()


class _ShortRead(Exception):
    pass
