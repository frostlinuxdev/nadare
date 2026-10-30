# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Published per-block checksums for streamed archives.

An index node may publish a *pin* for an archive: a sha256 digest of every
fixed-size block (default 1 MiB) of every part. A streamed install then checks
each block it reads against that list, so a CDN serving different bytes is
refused even when the archive's own CRCs are internally consistent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import indexfetch

if TYPE_CHECKING:
    from .directdl import Plan

log = logging.getLogger(__name__)

#: Usual block size for a pin: small enough to detect a changed member quickly,
#: large enough to keep the document and hashing cheap.
PIN_BLOCK = 1 << 20

#: How long a saved pin document may stand in when no mirror answers. Short on purpose: a
#: retracted or corrected pin must not linger for the generic 30 days.
PIN_CACHE_SECONDS = 2 * 24 * 3600

_MIN_BLOCK = 4096
_MAX_BLOCK = 16 << 20

#: The client refuses a pin document larger than this; the node job keeps to the same cap.
MAX_DOCUMENT_BYTES = 16 << 20


class PinError(ValueError):
    """A pin document is malformed or does not describe a usable pin."""


@dataclass(frozen=True)
class PinPart:
    """One archive part and the sha256 digest of each of its blocks."""

    url: str
    size: int
    blocks: tuple[bytes, ...]


@dataclass(frozen=True)
class ArchivePin:
    """All parts of one archive, pinned block by block."""

    block_size: int
    parts: tuple[PinPart, ...]
    publisher_md5: str | None = None

    def matches(
        self,
        parts: Sequence[Any],
        publisher_md5: str | None,
    ) -> bool:
        """True when ``parts`` and ``publisher_md5`` describe this pinned archive."""
        if len(parts) != len(self.parts):
            return False
        for pinned, actual in zip(self.parts, parts, strict=True):
            if pinned.url != getattr(actual, "url", None):
                return False
            actual_size = getattr(actual, "size", None)
            # Some sources do not know the archive size; the pin then supplies it, and a
            # wrong size on the server fails the block checks instead.
            if actual_size is not None and pinned.size != actual_size:
                return False
        if self.publisher_md5 and publisher_md5:
            return self.publisher_md5.lower() == publisher_md5.lower()
        return True

    def fingerprint(self) -> str:
        """A stable id of the whole table, so a corrected pin is not mistaken for the old one."""
        digest = hashlib.sha256(f"{self.block_size}".encode())
        for part in self.parts:
            digest.update(f"\n{part.url}\n{part.size}\n".encode())
            digest.update(b"".join(part.blocks))
        return digest.hexdigest()

    def block_hashes(self) -> list[list[bytes]]:
        """One digest list per part, in the shape :class:`RangeReader` expects."""
        return [list(part.blocks) for part in self.parts]


def hash_blocks(data: bytes, block_size: int) -> list[bytes]:
    """The sha256 of each ``block_size`` slice of ``data``, short tail included."""
    return [
        hashlib.sha256(data[offset : offset + block_size]).digest()
        for offset in range(0, len(data), block_size)
    ]


def pin_from_bytes(
    url: str,
    data: bytes,
    block_size: int,
    *,
    publisher_md5: str | None = None,
) -> ArchivePin:
    """Build a one-part pin for an archive whose bytes are known."""
    part = PinPart(url, len(data), tuple(hash_blocks(data, block_size)))
    return ArchivePin(block_size, (part,), publisher_md5)


def document(
    archives: Mapping[str, ArchivePin], *, generated_at: str | None = None
) -> dict[str, Any]:
    """The JSON-ready, deterministic form of ``archives``."""
    published: dict[str, Any] = {}
    for name in sorted(archives):
        pin = archives[name]
        entry: dict[str, Any] = {"block_size": pin.block_size}
        if pin.publisher_md5 is not None:
            entry["publisher_md5"] = pin.publisher_md5
        entry["parts"] = [
            {
                "url": part.url,
                "size": part.size,
                "blocks": base64.b64encode(b"".join(part.blocks)).decode("ascii"),
            }
            for part in pin.parts
        ]
        published[name] = entry
    result: dict[str, Any] = {"schema": 1, "archives": published}
    if generated_at is not None:
        result["generated_at"] = generated_at
    return result


def check_block_size(block_size: Any) -> None:
    """Refuse a block size the client cannot use."""
    if (
        not isinstance(block_size, int)
        or isinstance(block_size, bool)
        or block_size < _MIN_BLOCK
        or block_size > _MAX_BLOCK
        or block_size & (block_size - 1)
    ):
        raise PinError(f"invalid block size {block_size!r}")


def is_safe_pin_url(url: Any) -> bool:
    """True for https, or http on loopback: the only URLs a pin may carry."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _decode_blocks(value: Any, size: int, block_size: int) -> tuple[bytes, ...]:
    if not isinstance(value, str):
        raise PinError("part blocks must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PinError(f"part blocks are not valid base64: {exc}") from exc
    expected = 32 * ((size + block_size - 1) // block_size)
    if len(raw) != expected:
        raise PinError(f"part blocks decode to {len(raw)} bytes, expected {expected}")
    return tuple(raw[offset : offset + 32] for offset in range(0, len(raw), 32))


def _parse_archive(name: Any, value: Any) -> ArchivePin:
    if not isinstance(name, str) or not name:
        raise PinError("archive names must be non-empty strings")
    if not isinstance(value, Mapping):
        raise PinError(f"archive {name!r} is not a mapping")
    block_size = value.get("block_size")
    try:
        check_block_size(block_size)
    except PinError as exc:
        raise PinError(f"archive {name!r} has an invalid block size") from exc
    publisher_md5 = value.get("publisher_md5")
    if publisher_md5 is not None and not isinstance(publisher_md5, str):
        raise PinError(f"archive {name!r} has a non-string publisher md5")
    raw_parts = value.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise PinError(f"archive {name!r} has no parts")
    parts: list[PinPart] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, Mapping):
            raise PinError(f"archive {name!r} has a part that is not a mapping")
        url = raw_part.get("url")
        size = raw_part.get("size")
        if not is_safe_pin_url(url):
            raise PinError(f"archive {name!r} has an unsafe part URL")
        if not _positive_int(size):
            raise PinError(f"archive {name!r} has an invalid part size")
        parts.append(
            PinPart(
                url,
                size,
                _decode_blocks(raw_part.get("blocks"), size, block_size),
            )
        )
    return ArchivePin(block_size, tuple(parts), publisher_md5)


def parse_document(doc: Any) -> dict[str, ArchivePin]:
    """Parse and strictly validate a pin document."""
    if not isinstance(doc, Mapping):
        raise PinError("an archive-pin document must be a mapping")
    schema = doc.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != 1:
        raise PinError("unsupported archive-pin schema")
    archives = doc.get("archives")
    if not isinstance(archives, Mapping):
        raise PinError("archive-pin archives must be a mapping")
    return {name: _parse_archive(name, value) for name, value in archives.items()}


def attach(plan: Plan, pins: Mapping[str, ArchivePin]) -> Plan:
    """A copy of ``plan`` with each matching archive carrying its pin."""
    archives = []
    for archive in plan.archives:
        pin = pins.get(archive.name)
        parts = archive.parts or (archive.source,)
        if pin is not None and pin.matches(parts, archive.source.md5):
            archive = replace(archive, pin=pin)
        archives.append(archive)
    return replace(plan, archives=tuple(archives))


def _is_valid_document(doc: Any) -> bool:
    """For fetch_json: a mirror's document that does not parse is skipped, not cached."""
    try:
        parse_document(doc)
    except PinError:
        return False
    return True


#: A pin document is read once per operation: check/sync/verify each build a plan.
PIN_MEMO_SECONDS = 120.0
_memo: dict[str, tuple[float, dict[str, ArchivePin]]] = {}


def clear_cache() -> None:
    """Forget the pin documents read in this process (tests, and after an index refresh)."""
    _memo.clear()


def load(game_tag: str) -> dict[str, ArchivePin]:
    """Read ``catalog/archive-pins/<game_tag>.json``; never raise."""
    seen = _memo.get(game_tag)
    if seen is not None and time.monotonic() - seen[0] < PIN_MEMO_SECONDS:
        return seen[1]
    pins = _load(game_tag)
    _memo[game_tag] = (time.monotonic(), pins)
    return pins


def _load(game_tag: str) -> dict[str, ArchivePin]:
    if not game_tag or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for char in game_tag
    ):
        return {}
    rel = f"catalog/archive-pins/{game_tag}.json"
    try:
        result = indexfetch.fetch_json(
            rel,
            max_bytes=MAX_DOCUMENT_BYTES,
            max_cache_age=PIN_CACHE_SECONDS,
            validate=_is_valid_document,
        )
        return parse_document(result.data)
    except indexfetch.IndexUnavailable:
        return {}
    except Exception as exc:  # noqa: BLE001 - pins are optional, never fail an install
        log.warning("ignoring unusable archive pins %s: %s", rel, exc)
        return {}
