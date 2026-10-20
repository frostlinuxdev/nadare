# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal rendering of a QR PNG (``nadare login`` on a headless box).

The PNG builders here write real files with the standard library only, in the
colour types and scanline filters a server-generated QR image may use.
"""

from __future__ import annotations

import io
import struct
import sys
import zlib
from pathlib import Path

import pytest

from nadare.core import qr_text


def _finder(m: list[list[int]], top: int, left: int) -> None:
    for r in range(7):
        for c in range(7):
            ring = r in (0, 6) or c in (0, 6)
            core = 2 <= r <= 4 and 2 <= c <= 4
            m[top + r][left + c] = 1 if ring or core else 0


def _matrix(n: int = 25) -> list[list[int]]:
    """A QR-shaped matrix: three finder patterns plus some data modules."""
    m = [[0] * n for _ in range(n)]
    _finder(m, 0, 0)
    _finder(m, 0, n - 7)
    _finder(m, n - 7, 0)
    for i in range(8, n - 8):  # timing pattern
        m[6][i] = m[i][6] = 1 - (i % 2)
    for r in range(9, n - 9):
        for c in range(9, n - 9):
            m[r][c] = (r * 7 + c * 3) % 5 == 0
    return m


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _filter_row(kind: int, row: bytearray, prev: bytearray, bpp: int) -> bytes:
    out = bytearray([kind])
    for i, x in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        pred = {0: 0, 1: a, 2: b, 3: (a + b) // 2, 4: _paeth(a, b, c)}[kind]
        out.append((x - pred) & 0xFF)
    return bytes(out)


def make_png(
    matrix: list[list[int]],
    *,
    scale: int = 6,
    quiet: int = 4,
    color_type: int = 0,
    filter_kind: int = 0,
    interlace: int = 0,
) -> bytes:
    n = len(matrix)
    side = (n + 2 * quiet) * scale
    rows: list[list[int]] = []  # 1 = dark
    for y in range(side):
        my = y // scale - quiet
        line = []
        for x in range(side):
            mx = x // scale - quiet
            line.append(1 if 0 <= my < n and 0 <= mx < n and matrix[my][mx] else 0)
        rows.append(line)
    bit_depth = 8
    plte = b""
    if color_type == 0:
        bpp = 1
        raw = [bytearray(0 if v else 255 for v in r) for r in rows]
    elif color_type == 2:
        bpp = 3
        raw = [
            bytearray(b for v in r for b in ((10, 10, 10) if v else (250, 250, 250)))
            for r in rows
        ]
    elif color_type == 3:
        bpp = 1
        plte = _chunk(b"PLTE", bytes([255, 255, 255, 0, 0, 0]))  # 0 light, 1 dark
        raw = [bytearray(r) for r in rows]
    elif color_type == 4:
        bpp = 2
        raw = [bytearray(b for v in r for b in ((0, 255) if v else (255, 255))) for r in rows]
    elif color_type == 6:
        bpp = 4
        raw = [
            bytearray(b for v in r for b in ((0, 0, 0, 255) if v else (255, 255, 255, 0)))
            for r in rows
        ]
    else:  # pragma: no cover
        raise AssertionError(color_type)
    prev = bytearray(len(raw[0]))
    data = b""
    for row in raw:
        data += _filter_row(filter_kind, row, prev, bpp)
        prev = row
    ihdr = struct.pack(">IIBBBBB", side, side, bit_depth, color_type, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + plte
        + _chunk(b"IDAT", zlib.compress(data))
        + _chunk(b"IEND", b"")
    )


def _modules(text: str) -> list[list[int]]:
    """Invert render(): ANSI half-block text -> module matrix (1 = dark)."""
    out: list[list[int]] = []
    for line in text.splitlines():
        cells = [seg for seg in line.split("\x1b[")[1:] if seg and not seg.startswith("0m")]
        top, bottom = [], []
        for seg in cells:
            sgr = seg.split("m", 1)[0]
            fg, bg = (int(p) for p in sgr.split(";"))
            top.append(1 if fg == 30 else 0)
            bottom.append(1 if bg == 40 else 0)
        out.append(top)
        out.append(bottom)
    return out


@pytest.mark.parametrize("color_type", [0, 2, 3, 4, 6])
@pytest.mark.parametrize("filter_kind", [0, 1, 2, 3, 4])
def test_round_trip_every_colour_type_and_filter(tmp_path: Path, color_type, filter_kind):
    m = _matrix()
    png = tmp_path / "q.png"
    png.write_bytes(make_png(m, color_type=color_type, filter_kind=filter_kind))
    text = qr_text.render_png(png)
    assert text is not None
    rows = _modules(text)
    q = qr_text.QUIET
    got = [r[q : q + len(m)] for r in rows[q : q + len(m)]]
    assert got == [[1 if v else 0 for v in r] for r in m]


def test_module_size_is_detected_not_assumed(tmp_path: Path):
    m = _matrix(21)
    for scale in (1, 3, 8):
        png = tmp_path / f"q{scale}.png"
        png.write_bytes(make_png(m, scale=scale))
        rows = _modules(qr_text.render_png(png))
        q = qr_text.QUIET
        assert [r[q : q + 21] for r in rows[q : q + 21]] == [[1 if v else 0 for v in r] for r in m]


def test_output_is_theme_independent(tmp_path: Path):
    png = tmp_path / "q.png"
    png.write_bytes(make_png(_matrix()))
    text = qr_text.render_png(png)
    # explicit black/white colours on every cell, always reset at line end
    assert "\x1b[30;107m" in text or "\x1b[97;40m" in text
    assert all(line.endswith("\x1b[0m") for line in text.splitlines())


@pytest.mark.parametrize("payload", [b"", b"not a png", b"\x89PNG\r\n\x1a\n"])
def test_garbage_returns_none(tmp_path: Path, payload):
    p = tmp_path / "bad.png"
    p.write_bytes(payload)
    assert qr_text.render_png(p) is None


def test_missing_file_and_interlaced_return_none(tmp_path: Path):
    assert qr_text.render_png(tmp_path / "nope.png") is None
    p = tmp_path / "i.png"
    p.write_bytes(make_png(_matrix(), interlace=1))
    assert qr_text.render_png(p) is None


def test_blank_image_returns_none(tmp_path: Path):
    p = tmp_path / "blank.png"
    p.write_bytes(make_png([[0] * 21 for _ in range(21)]))
    assert qr_text.render_png(p) is None


def test_oversized_image_is_refused(tmp_path: Path):
    p = tmp_path / "big.png"
    ihdr = struct.pack(">IIBBBBB", 60000, 60000, 8, 0, 0, 0, 0)
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\0"))
        + _chunk(b"IEND", b"")
    )
    assert qr_text.render_png(p) is None


# --- CLI printer --------------------------------------------------------------
class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _event(png: Path):
    from nadare.core.events import Event

    return Event("step", "Scan the QR code with the STOVE app", data={"png": str(png), "url": "u"})


def test_cli_printer_draws_qr_on_a_tty(tmp_path: Path, monkeypatch):
    from nadare import app

    png = tmp_path / "q.png"
    png.write_bytes(make_png(_matrix()))
    out = _Tty()
    monkeypatch.setattr(sys, "stdout", out)
    app._make_printer()(_event(png))
    text = out.getvalue()
    assert "[step] Scan the QR code" in text
    assert "\x1b[" in text and "▀" in text


def test_cli_printer_stays_plain_when_not_a_tty(tmp_path: Path, monkeypatch):
    from nadare import app

    png = tmp_path / "q.png"
    png.write_bytes(make_png(_matrix()))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    app._make_printer()(_event(png))
    assert "\x1b[" not in out.getvalue()
    assert "▀" not in out.getvalue()


def test_cli_printer_survives_unreadable_png(tmp_path: Path, monkeypatch):
    from nadare import app

    out = _Tty()
    monkeypatch.setattr(sys, "stdout", out)
    app._make_printer()(_event(tmp_path / "missing.png"))
    assert "[step] Scan the QR code" in out.getvalue()
