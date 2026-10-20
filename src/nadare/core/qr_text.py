# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Draw a QR code PNG in a terminal (``nadare login`` over SSH / headless).

The login server already returns the QR as a PNG, so this decodes that image
with the standard library (no QR encoder, no new dependency) and prints it as
ANSI half-blocks. Colours are explicit black/white, so it scans on both dark
and light terminal themes. Anything unexpected returns ``None`` and the caller
falls back to the printed file path/URL.

The QR is a login credential: callers print it to an interactive terminal only
and never log it.
"""

from __future__ import annotations

import struct
import zlib
from math import gcd
from pathlib import Path

#: Light modules around the code, in modules (the QR spec asks for 4; 2 scans).
QUIET = 2

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SIDE = 4096
_MAX_FILE = 4 << 20
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> list[bytearray] | None:
    rows: list[bytearray] = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        if pos + 1 + stride > len(raw):
            return None
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if kind == 0:
            pass
        elif kind in (1, 2, 3, 4):
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                if kind == 1:
                    pred = a
                elif kind == 2:
                    pred = b
                elif kind == 3:
                    pred = (a + b) >> 1
                else:
                    c = prev[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 0xFF
        else:
            return None
        rows.append(line)
        prev = line
    return rows


def _decode(data: bytes) -> list[list[bool]] | None:
    """PNG bytes -> rows of ``True`` (dark) / ``False`` (light), or ``None``."""
    if not data.startswith(_SIGNATURE):
        return None
    pos = len(_SIGNATURE)
    ihdr = None
    palette: list[tuple[int, int, int]] = []
    idat = bytearray()
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length:
            return None
        pos += 12 + length
        if kind == b"IHDR" and length == 13:
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = [tuple(body[i : i + 3]) for i in range(0, len(body) - 2, 3)]  # type: ignore[misc]
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if ihdr is None:
        return None
    width, height, depth, ctype, _comp, _filt, interlace = ihdr
    if interlace != 0 or ctype not in _CHANNELS or depth not in (1, 2, 4, 8, 16):
        return None
    if ctype in (2, 4, 6) and depth < 8:
        return None
    if not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
        return None
    channels = _CHANNELS[ctype]
    bits = channels * depth
    stride = (width * bits + 7) // 8
    bpp = max(1, bits // 8)
    try:
        raw = zlib.decompress(bytes(idat), 15, stride * height + height + 1024)
    except (zlib.error, ValueError):
        return None
    rows = _unfilter(raw, height, stride, bpp)
    if rows is None:
        return None
    out: list[list[bool]] = []
    for line in rows:
        px: list[bool] = []
        for x in range(width):
            if depth == 8:
                base = x * channels
                v = line[base : base + channels]
            elif depth == 16:
                base = x * channels * 2
                v = bytes(line[base + 2 * k] for k in range(channels))
            else:
                bit = x * depth
                idx = (line[bit >> 3] >> (8 - depth - (bit & 7))) & ((1 << depth) - 1)
                v = bytes([idx * 255 // ((1 << depth) - 1)]) if ctype == 0 else bytes([idx])
            if ctype == 0:
                lum, alpha = v[0], 255
            elif ctype == 4:
                lum, alpha = v[0], v[1]
            elif ctype == 2:
                lum, alpha = (v[0] + v[1] + v[2]) // 3, 255
            elif ctype == 6:
                lum, alpha = (v[0] + v[1] + v[2]) // 3, v[3]
            else:
                if v[0] >= len(palette):
                    return None
                r, g, b = palette[v[0]]
                lum, alpha = (r + g + b) // 3, 255
            px.append(alpha >= 128 and lum < 128)
        out.append(px)
    return out


def _modules(pixels: list[list[bool]]) -> list[list[bool]] | None:
    """Crop to the code and sample one value per module."""
    dark_rows = [y for y, row in enumerate(pixels) if any(row)]
    if not dark_rows:
        return None
    top, bottom = dark_rows[0], dark_rows[-1]
    cols = [x for x in range(len(pixels[0])) if any(pixels[y][x] for y in range(top, bottom + 1))]
    left, right = cols[0], cols[-1]
    width, height = right - left + 1, bottom - top + 1
    if width != height or width < 21:
        return None
    # Every horizontal run is a whole number of modules; the finder pattern's
    # rows contain 1-module runs, so their gcd is the module size. One row is
    # not enough (a version-1 code's top row is 7+7+7 modules) and the finder is
    # seven modules tall, i.e. at most a third of the image.
    size = 0
    for y in range(top, min(top + width // 3 + 1, bottom + 1)):
        row = pixels[y][left : right + 1]
        run = 1
        for i in range(1, len(row)):
            if row[i] == row[i - 1]:
                run += 1
            else:
                size = gcd(size, run)
                run = 1
        size = gcd(size, run)
    if size < 1 or width % size:
        return None
    n = width // size
    if n < 21 or (n - 17) % 4:  # QR sizes are 21, 25, 29, ...
        return None
    half = size // 2
    return [
        [pixels[top + r * size + half][left + c * size + half] for c in range(n)]
        for r in range(n)
    ]


def render(modules: list[list[bool]]) -> str:
    """Module matrix -> ANSI half-block text (two module rows per line)."""
    n = len(modules)
    q = QUIET
    side = n + 2 * q

    def cell(r: int, c: int) -> bool:
        r, c = r - q, c - q
        return 0 <= r < n and 0 <= c < n and modules[r][c]

    lines = []
    for r in range(0, side, 2):
        parts = []
        for c in range(side):
            top = cell(r, c)
            bottom = cell(r + 1, c) if r + 1 < side else False
            parts.append(f"\x1b[{30 if top else 97};{40 if bottom else 107}m▀")
        lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)


def render_png(path: str | Path) -> str | None:
    """Terminal text for the QR PNG at ``path``, or ``None`` if unusable."""
    try:
        p = Path(path)
        if p.stat().st_size > _MAX_FILE:
            return None
        pixels = _decode(p.read_bytes())
    except (OSError, ValueError, struct.error, IndexError, MemoryError):
        return None
    if pixels is None:
        return None
    try:
        modules = _modules(pixels)
    except IndexError:
        return None
    return render(modules) if modules else None
