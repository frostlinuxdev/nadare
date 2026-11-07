# SPDX-License-Identifier: GPL-3.0-or-later
"""Decompression is bounded by what the caller allows, not by what a frame declares (#863, #864)."""

from __future__ import annotations

import hashlib
import struct
import zlib

import pytest

from nadare.core import directdl as dd
from nadare.core import sophon
from nadare.core.sources import nte

zstandard = pytest.importorskip("zstandard")


def _frame(data: bytes) -> bytes:
    """A zstd frame that declares its content size (what the library normally writes)."""
    return zstandard.ZstdCompressor().compress(data)


def _chunk(size: int, payload: bytes) -> sophon.ChunkEntry:
    return sophon.ChunkEntry(
        name="c",
        md5=hashlib.md5(payload).hexdigest(),
        offset=0,
        size=size,
        csize=len(_frame(payload)),
        cmd5="",
    )


class _Pool:
    pass


# ---- Sophon: a frame that declares a big content size is cut off at the cap ----------------
def test_a_chunk_frame_bigger_than_its_declared_size_is_rejected(monkeypatch):
    payload = b"\0" * (2 << 20)  # a tiny frame that expands to 2 MiB
    frame = _frame(payload)
    assert len(frame) < 100
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: frame)
    with pytest.raises(sophon.SophonError, match="larger|decompress"):
        sophon._fetch_chunk(_Pool(), _pkg(), _chunk(1, payload), None)


def test_a_chunk_of_exactly_its_declared_size_still_decodes(monkeypatch):
    payload = b"abc" * 100
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: _frame(payload))
    assert sophon._fetch_chunk(_Pool(), _pkg(), _chunk(len(payload), payload), None) == payload


def test_an_empty_chunk_is_allowed_and_a_non_empty_one_claiming_zero_is_not(monkeypatch):
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: _frame(b""))
    assert sophon._fetch_chunk(_Pool(), _pkg(), _chunk(0, b""), None) == b""
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: _frame(b"x" * 10))
    with pytest.raises(sophon.SophonError):
        sophon._fetch_chunk(_Pool(), _pkg(), _chunk(0, b"x" * 10), None)


def test_a_manifest_that_unpacks_past_the_cap_is_rejected(monkeypatch):
    big = b"\0" * 4096
    monkeypatch.setattr(sophon, "_MAX_MANIFEST_UNPACKED", 64)
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: _frame(big))
    with pytest.raises(sophon.SophonError, match="larger|decompress|manifest"):
        sophon._load_manifest(_Pool(), _pkg())


def test_the_decoder_reads_at_most_one_byte_past_the_cap(monkeypatch):
    asked: list[int] = []

    class Reader:
        def __init__(self, inner):
            self.inner = inner

        def read(self, size=-1):
            asked.append(size)
            return self.inner.read(size)

    real = sophon._zstd()

    class Decompressor:
        def stream_reader(self, source, *a, **k):
            return Reader(real.ZstdDecompressor().stream_reader(source, *a, **k))

        def decompress(self, *a, **k):  # the unbounded call must not be used
            raise AssertionError("decompress() trusts the frame's declared size")

    class Module:
        ZstdError = real.ZstdError

        @staticmethod
        def ZstdDecompressor():  # noqa: N802
            return Decompressor()

    monkeypatch.setattr(sophon, "_zstd", lambda: Module)
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: _frame(b"hello"))
    assert sophon._fetch_chunk(_Pool(), _pkg(), _chunk(5, b"hello"), None) == b"hello"
    assert asked and max(asked) == 6


def _pkg():
    return sophon.SophonPackage(
        name="p",
        manifest_url="https://example.invalid/m",
        chunk_url="https://example.invalid/c",
        manifest_id="id",
        size=0,
    )


# ---- NTE: a declared length of zero is not "unlimited" ----------------------------------
def _container(stored: int, inflated: bytes, monkeypatch) -> bytes:
    plain = zlib.compress(inflated)
    padded_len = (len(plain) // 16 + 1) * 16
    body = plain.ljust(padded_len, b"\0")
    monkeypatch.setattr(nte._aes, "aes128_cbc_decrypt", lambda key, iv, data: plain)
    monkeypatch.setattr(nte._aes, "pkcs7_unpad", lambda data: data)
    return nte.MAGIC + struct.pack("<I", stored) + body


def test_a_zero_declared_length_is_refused_before_inflating(monkeypatch):
    data = _container(0, b"<x/>" * (1 << 19), monkeypatch)  # about 2 MiB of XML
    inflated: list[int] = []
    real = zlib.decompressobj

    class Watch:
        def __init__(self):
            self.inner = real()

        def decompress(self, data, max_length=0):
            inflated.append(max_length)
            return self.inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(nte.zlib, "decompressobj", Watch)
    with pytest.raises(dd.DirectError):
        nte.decode_container(data, "1")
    assert 0 not in inflated, "max_length=0 means no limit"


def test_a_normal_container_still_decodes(monkeypatch):
    xml = b"<root><a/></root>"
    data = _container(len(xml), xml, monkeypatch)
    assert nte.decode_container(data, "1") == xml.decode()
