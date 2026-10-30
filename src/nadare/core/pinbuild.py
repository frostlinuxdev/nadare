# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Build a block pin while an archive is streamed, without holding it in memory."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from . import archivepins

_HEX = frozenset("0123456789abcdefABCDEF")


class PinBuildError(ValueError):
    """A stream cannot be pinned, or does not match the publisher's metadata."""


def _check_publisher_md5(publisher_md5: str | None) -> None:
    if publisher_md5 is None:
        return
    if (
        not isinstance(publisher_md5, str)
        or len(publisher_md5) != 32
        or any(char not in _HEX for char in publisher_md5)
    ):
        raise PinBuildError("publisher md5 must be 32 hex digits")


def build_pin(
    url: str,
    chunks: Iterable[bytes],
    *,
    block_size: int = archivepins.PIN_BLOCK,
    size: int | None = None,
    publisher_md5: str | None = None,
    max_bytes: int | None = None,
) -> archivepins.ArchivePin:
    """Hash ``chunks`` once into per-block sha256 digests and a whole md5."""
    try:
        archivepins.check_block_size(block_size)
    except archivepins.PinError as exc:
        raise PinBuildError(str(exc)) from exc
    _check_publisher_md5(publisher_md5)
    md5 = hashlib.md5()
    blocks: list[bytes] = []
    buffer = bytearray()
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise PinBuildError("stream chunks must be bytes")
        if not chunk:
            continue
        total += len(chunk)
        if size is not None and total > size:
            raise PinBuildError(f"stream exceeds the declared size {size}")
        if max_bytes is not None and total > max_bytes:
            raise PinBuildError(f"stream exceeds the byte limit of {max_bytes}")
        md5.update(chunk)
        buffer.extend(chunk)
        while len(buffer) >= block_size:
            blocks.append(hashlib.sha256(buffer[:block_size]).digest())
            del buffer[:block_size]
    if buffer:
        blocks.append(hashlib.sha256(buffer).digest())
    if total == 0:
        raise PinBuildError("empty stream")
    if size is not None and total != size:
        raise PinBuildError(f"stream size {total} does not match the declared size {size}")
    if publisher_md5 is not None and md5.hexdigest() != publisher_md5.lower():
        raise PinBuildError("stream md5 does not match the publisher md5")
    part = archivepins.PinPart(url, total, tuple(blocks))
    return archivepins.ArchivePin(
        block_size,
        (part,),
        publisher_md5.lower() if publisher_md5 is not None else None,
    )
