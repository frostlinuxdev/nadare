# SPDX-License-Identifier: GPL-3.0-or-later
"""A tiny encoder for Kuro-style .krpdiff test patches (#1252)."""

from __future__ import annotations

import zstandard


def varint(value: int, tag_bits: int = 0, tag: int = 0) -> bytes:
    width = 7 - tag_bits
    if value < (1 << width):
        return bytes([((tag << 7) if tag_bits else 0) | value])
    chunks = [value & 0x7F]
    value >>= 7
    while value >= (1 << width):
        chunks.append(value & 0x7F)
        value >>= 7
    first = value | (1 << width) | ((tag << 7) if tag_bits else 0)
    rest = [c | 0x80 for c in reversed(chunks[1:])] + [chunks[0]]
    return bytes([first, *rest])


def section(raw: bytes, compress: bool) -> tuple[bytes, int, int]:
    if not compress:
        return raw, len(raw), 0
    body = zstandard.ZstdCompressor().compress(raw)
    return body, len(raw), len(body)


def build(
    old: list[tuple[str, bytes]],
    new: list[tuple[str, bytes]],
    ops: list[tuple],
    *,
    compress: bool = True,
    dirs: tuple[str, ...] = (),
    wrap: bool = False,
) -> bytes:
    """``ops``: ("new", bytes) or ("old", position, length), in output order."""
    old_blob = b"".join(b for _, b in old)
    old_size, new_size = len(old_blob), sum(len(b) for _, b in new)
    new_paths = list(dirs) + [p for p, _ in new]
    new_offsets = []
    previous = -1
    for i in range(len(dirs), len(new_paths)):
        new_offsets.append(i - previous - 1 if new_offsets else i)
        previous = i
    head = b"".join(p.encode() + b"\0" for p, _ in old)
    head += b"".join(p.encode() + b"\0" for p in new_paths)
    head += b"".join(varint(0) for _ in old)  # old files are paths 0..n-1
    head += b"".join(varint(o) for o in new_offsets)
    head += b"".join(varint(len(b)) for _, b in old)
    head += b"".join(varint(len(b)) for _, b in new)
    head += b"".join(varint(0) for _ in new)  # checksums (not used by Nadare)
    head_body, head_raw, head_comp = section(head, compress)
    hd19 = b"HDIFF19&zstd&fadler64\0\x01\x01" + b"".join(
        varint(v)
        for v in (
            len(old), 0, len(new_paths), 0, len(old), old_size, len(new), new_size,
            0, 0, 0, 0, 0, 0, head_raw, head_comp, 0,
        )
    ) + head_body

    covers, data = b"", b""
    position, pending, count = 0, b"", 0
    for op in ops:
        if op[0] == "new":
            pending += op[1]
            continue
        _, at, length = op
        delta = at - position
        if wrap and delta >= 0:
            delta += old_size  # same place after the wrap
        sign = 1 if delta < 0 else 0
        covers += varint(abs(delta), 1, sign) + varint(len(pending)) + varint(length)
        data += pending
        pending = b""
        position = at + length
        count += 1
    data += pending
    cover_body, cover_raw, cover_comp = section(covers, compress)
    data_body, data_raw, data_comp = section(data, compress)
    hd13 = b"HDIFF13&zstd\0" + b"".join(
        varint(v)
        for v in (new_size, old_size, count, cover_raw, cover_comp, 0, 0, 0, 0, data_raw, data_comp)
    )
    return hd19 + hd13 + cover_body + data_body


