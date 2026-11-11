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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import hosting, indexfetch

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


class PinMismatch(Exception):
    """The publisher's file at a pinned URL is not the file that was pinned (#1158)."""


#: The one line shown when an install goes ahead without its published checksums.
NOT_CHECKED = "Not checked against a published checksum: {why}. Installing anyway."


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


def verify_file(path: Path, part: PinPart, block_size: int) -> None:
    """Check ``path`` against ``part`` one block at a time, raising :class:`PinError`."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PinError(f"cannot read {path.name}: {exc}") from exc
    if size != part.size:
        raise PinError(
            f"{path.name} does not match the published checksum list "
            f"(size {size}, expected {part.size})"
        )
    expected = iter(part.blocks)
    try:
        with open(path, "rb") as fh:
            while block := fh.read(block_size):
                wanted = next(expected, None)
                if wanted is None or hashlib.sha256(block).digest() != wanted:
                    raise PinError(f"{path.name} does not match the published checksum list")
    except OSError as exc:
        raise PinError(f"cannot read {path.name}: {exc}") from exc
    if next(expected, None) is not None:
        raise PinError(f"{path.name} does not match the published checksum list")


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


def _same_location(pin: ArchivePin, parts: Sequence[Any]) -> bool:
    """The pin names exactly these URLs: it is about this very file, not another version."""
    return len(parts) == len(pin.parts) and all(
        pinned.url == getattr(actual, "url", None)
        for pinned, actual in zip(pin.parts, parts, strict=True)
    )


def _describe(parts: Sequence[Any], md5: str | None) -> str:
    sizes = ", ".join(str(getattr(part, "size", None)) for part in parts)
    return f"size {sizes}" + (f", md5 {md5.lower()}" if md5 else "")


def check(plan: Plan, loaded: Loaded) -> tuple[Plan, list[str]]:
    """Attach the matching pins; raise :class:`PinMismatch` for a changed pinned file.

    Returns the plan and at most one note for the user when some archive goes unchecked.
    A pin naming another URL is for another version (the pin job lags a game update), so
    that archive is installed unchecked rather than refused.
    """
    archives = []
    why: str | None = None
    for archive in plan.archives:
        parts = archive.parts or (archive.source,)
        md5 = archive.source.md5
        pin = loaded.pins.get(archive.name)
        if pin is not None and pin.matches(parts, md5):
            archive = replace(archive, pin=pin)
        elif pin is not None and _same_location(pin, parts):
            pinned = [part.size for part in pin.parts]
            raise PinMismatch(
                f"The publisher's file for {archive.name} changed since Nadare last checked it "
                f"(expected size {', '.join(map(str, pinned))}"
                + (f", md5 {pin.publisher_md5.lower()}" if pin.publisher_md5 else "")
                + f"; found {_describe(parts, md5)}). Nothing was installed. Try again later; "
                "if it keeps happening, report it."
            )
        elif pin is not None:
            why = why or "the published checksums are for another version of this game"
        elif loaded.available:
            why = why or f"there is no published checksum for {archive.name} yet"
        elif loaded.known:
            why = why or "the published checksums could not be read right now"
        archives.append(archive)
    notes = [NOT_CHECKED.format(why=why)] if why else []
    return replace(plan, archives=tuple(archives)), notes


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


@dataclass(frozen=True)
class Loaded:
    """A game's pins and what is known about them.

    ``available``: a usable document was read now (or a fresh enough saved copy).
    ``known``: this game has pins: a document was read now, or one was saved on this
    machine before (any age, even unusable). Games that never had pins stay quiet.
    """

    pins: dict[str, ArchivePin]
    available: bool
    known: bool


#: A pin document is read once per operation: check/sync/verify each build a plan.
PIN_MEMO_SECONDS = 120.0
_memo: dict[str, tuple[float, Loaded]] = {}


def clear_cache() -> None:
    """Forget the pin documents read in this process (tests, and after an index refresh)."""
    _memo.clear()


def load(game_tag: str) -> dict[str, ArchivePin]:
    """Read ``catalog/archive-pins/<game_tag>.json``; never raise."""
    return load_state(game_tag).pins


def load_state(game_tag: str) -> Loaded:
    """:func:`load` plus whether the pins were readable and whether the game has any."""
    seen = _memo.get(game_tag)
    if seen is not None and time.monotonic() - seen[0] < PIN_MEMO_SECONDS:
        return seen[1]
    state = _load(game_tag)
    _memo[game_tag] = (time.monotonic(), state)
    return state


def _saved_before(rel: str) -> bool:
    """A copy of this document was saved on this machine once (any age)."""
    try:
        return any(
            indexfetch.cache_path(base, rel).exists()
            for base in hosting.index_bases()
            if base.startswith("https://")
        )
    except Exception:  # noqa: BLE001 - only decides whether a note is shown
        return False


def _load(game_tag: str) -> Loaded:
    if not game_tag or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for char in game_tag
    ):
        return Loaded({}, available=False, known=False)
    rel = f"catalog/archive-pins/{game_tag}.json"
    try:
        result = indexfetch.fetch_json(
            rel,
            max_bytes=MAX_DOCUMENT_BYTES,
            max_cache_age=PIN_CACHE_SECONDS,
            validate=_is_valid_document,
        )
        return Loaded(parse_document(result.data), available=True, known=True)
    except indexfetch.IndexUnavailable:
        return Loaded({}, available=False, known=_saved_before(rel))
    except Exception as exc:  # noqa: BLE001 - pins are optional, never fail an install
        log.warning("ignoring unusable archive pins %s: %s", rel, exc)
        return Loaded({}, available=False, known=True)
