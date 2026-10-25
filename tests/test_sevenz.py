# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.sevenz against real archives made with the 7z tool."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core import sevenz
from nadare.core.events import Cancel, Cancelled

FIX = Path(__file__).parent / "fixtures" / "sevenz"
EXPECTED = FIX / "expected"
LAYOUTS = ["lzma2", "lzma1", "copy", "bcj", "plainhdr", "nonsolid"]


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def open7z(name: str):
    return open(FIX / f"{name}.7z", "rb")


@pytest.mark.parametrize("name", LAYOUTS)
def test_list_entries(name):
    with open7z(name) as fh:
        entries = sevenz.list_entries(fh)
    files = {e.path: e.size for e in entries if not e.is_dir}
    expected = {k: len(v) for k, v in tree(EXPECTED).items()}
    assert files == expected
    assert {e.path for e in entries if e.is_dir} == {"sub", "sub/deep"}


def test_extract_refuses_existing_symlinked_destination_parent(tmp_path):
    dest = tmp_path / "install"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    original = outside / "data.bin"
    original.write_bytes(b"USERDATA")
    (dest / "sub").symlink_to(outside, target_is_directory=True)

    with open7z("lzma2") as fh, pytest.raises(sevenz.SevenZipError, match="symlink|outside"):
        sevenz.extract(fh, dest)

    assert original.read_bytes() == b"USERDATA"


@pytest.mark.parametrize("name", LAYOUTS)
def test_extract_matches_the_original_tree(name, tmp_path):
    with open7z(name) as fh:
        n = sevenz.extract(fh, tmp_path)
    assert tree(tmp_path) == tree(EXPECTED)
    assert n == len(tree(EXPECTED))
    assert (tmp_path / "sub" / "deep").is_dir()
    assert (tmp_path / "empty.bin").read_bytes() == b""
    assert (tmp_path / "uni-ファイル-é.txt").read_text() == "unicode name\n"


def test_split_volumes_read_as_one_file(tmp_path):
    parts = [FIX / "split.7z.001", FIX / "split.7z.002"]
    joined = dd._JoinedFile(parts)
    try:
        sevenz.extract(joined, tmp_path)
    finally:
        joined.close()
    assert tree(tmp_path) == tree(EXPECTED)


def test_only_filter_and_progress(tmp_path):
    seen: list[tuple[int, int]] = []
    with open7z("lzma2") as fh:
        n = sevenz.extract(fh, tmp_path, only=lambda p: p.startswith("sub/"), progress=lambda a, b: seen.append((a, b)))
    assert set(tree(tmp_path)) == {"sub/data.bin", "sub/deep/text.txt"}
    assert n == 2
    assert seen and seen[-1][0] <= seen[-1][1] and all(b >= a for a, b in seen)


def test_cancel_is_honoured(tmp_path):
    cancel = Cancel()
    cancel.cancel()
    with open7z("lzma2") as fh, pytest.raises(Cancelled):
        sevenz.extract(fh, tmp_path, cancel=cancel)


def test_crc_mismatch_raises_and_removes_the_bad_file(tmp_path):
    data = bytearray((FIX / "copy.7z").read_bytes())
    marker = b"hello world\nhello world\n"
    i = bytes(data).find(marker)
    assert i > 0  # stored uncompressed by the copy coder
    data[i] ^= 0xFF
    with pytest.raises(sevenz.SevenZipError, match="(?i)crc"):
        sevenz.extract(io.BytesIO(bytes(data)), tmp_path)
    assert not (tmp_path / "hello.txt").exists()


def test_a_path_that_escapes_is_rejected_and_nothing_is_written(tmp_path):
    dest = tmp_path / "out"
    with open7z("traversal") as fh, pytest.raises(sevenz.SevenZipError):
        sevenz.extract(fh, dest)
    assert not (tmp_path / "yy.txt").exists()
    assert not dest.exists() or not any(dest.rglob("*.txt"))


def test_symlinks_are_refused(tmp_path):
    with open7z("symlink") as fh, pytest.raises(sevenz.SevenZipError, match="(?i)symlink|link"):
        sevenz.extract(fh, tmp_path)
    assert not (tmp_path / "link.txt").exists()


def test_not_a_7z_and_truncated_files(tmp_path):
    for blob in (b"", b"PK\x03\x04" + b"0" * 100, b"7z\xbc\xaf\x27\x1c" + b"\x00" * 10):
        with pytest.raises(sevenz.SevenZipError):
            sevenz.list_entries(io.BytesIO(blob))
    good = (FIX / "lzma2.7z").read_bytes()
    for cut in (len(good) - 5, len(good) // 2, 40):
        with pytest.raises(sevenz.SevenZipError):
            sevenz.extract(io.BytesIO(good[:cut]), tmp_path / f"t{cut}")


def test_a_damaged_header_is_detected():
    data = bytearray((FIX / "plainhdr.7z").read_bytes())
    _crc, off, size, _hcrc = struct.unpack("<IQQI", data[8:32])
    data[32 + off + size // 2] ^= 0x55  # inside the header; its CRC no longer matches
    with pytest.raises(sevenz.SevenZipError):
        sevenz.list_entries(io.BytesIO(bytes(data)))


def test_an_unsupported_coder_is_reported_by_name():
    # Build a tiny, valid-CRC archive whose only coder id is PPMd (030401).
    streams = bytes([0x06, 0x00, 0x01, 0x09, 0x04, 0x00])  # PackInfo: pos 0, 1 stream, size 4
    unpack = bytes([0x07, 0x0B, 0x01, 0x00, 0x01, 0x23, 0x03, 0x04, 0x01, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00,
                    0x0C, 0x04, 0x00])
    header = bytes([0x01, 0x04]) + streams + unpack + bytes([0x00, 0x00])
    body = b"\x00" * 4
    start = struct.pack("<QQI", len(body), len(header), zlib.crc32(header) & 0xFFFFFFFF)
    sig = b"7z\xbc\xaf\x27\x1c" + b"\x00\x04" + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF) + start
    with pytest.raises(sevenz.SevenZipError):
        sevenz.list_entries(io.BytesIO(sig + body + header))


def _blob(n: int, k: int) -> bytes:
    return bytes(((i * 31 + (i >> 7) + k) & 0xFF) for i in range(n))


BIG = {
    "a.bin": _blob(2_500_000, 1),
    "b.bin": _blob(1_048_576, 2),  # exactly one read chunk
    "c.bin": _blob(3_000_017, 3),
    "d.txt": b"tiny file at the end",
}


def test_files_larger_than_a_read_chunk_extract_correctly(tmp_path):
    """Regression: progress inside a file spanning several chunks used to be reset."""
    with open7z("bigfiles") as fh:
        assert sevenz.extract(fh, tmp_path) == len(BIG)
    assert tree(tmp_path) == BIG


@pytest.mark.parametrize("wanted", [{"b.bin"}, {"a.bin", "d.txt"}, {"c.bin"}, {"a.bin"}])
def test_only_with_large_files_and_early_stop(tmp_path, wanted):
    with open7z("bigfiles") as fh:
        sevenz.extract(fh, tmp_path, only=lambda p: p in wanted)
    assert tree(tmp_path) == {k: BIG[k] for k in wanted}


@pytest.mark.parametrize("name", LAYOUTS)
def test_entries_carry_the_crc_and_folder(name):
    expected = tree(EXPECTED)
    with open7z(name) as fh:
        entries = [e for e in sevenz.list_entries(fh) if not e.is_dir]
    for e in entries:
        data = expected[e.path]
        if data:
            assert e.crc == zlib.crc32(data) & 0xFFFFFFFF, e.path
            assert e.folder is not None
        else:
            assert not e.crc  # an empty file has no stream
    if name == "nonsolid":
        assert len({e.folder for e in entries if e.size}) > 1  # several independent folders
