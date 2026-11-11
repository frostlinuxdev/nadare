# SPDX-License-Identifier: GPL-3.0-or-later
"""Build synthetic "rail_setup" repositories for tests: no publisher data is stored.

The encoders mirror what the readers in ``nadare.core.railrepo`` undo: QQTEA with the public pi
key, a zstd body, protobuf messages and AES-128-CBC file names (zero IV, PKCS7).
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import struct

import zstandard
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PI_KEY = b"3.14159265358979"
NAME_KEY = "00112233445566778899aabbccddeeff"
_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9


# ---- QQTEA -------------------------------------------------------------------------------
def _tea_encrypt_block(block: bytes, key: bytes) -> bytes:
    a, b = struct.unpack(">2I", block)
    k = struct.unpack(">4I", key)
    s = 0
    for _ in range(16):
        s = (s + _DELTA) & _MASK
        a = (
            a
            + ((((b << 4) + k[0]) & _MASK) ^ ((b + s) & _MASK) ^ (((b >> 5) + k[1]) & _MASK))
        ) & _MASK
        b = (
            b
            + ((((a << 4) + k[2]) & _MASK) ^ ((a + s) & _MASK) ^ (((a >> 5) + k[3]) & _MASK))
        ) & _MASK
    return struct.pack(">2I", a, b)


def qqtea_encrypt(plain: bytes, key: bytes = PI_KEY) -> bytes:
    fill = (8 - (len(plain) + 10) % 8) % 8
    data = bytes([0xF8 & 0xA8 | fill]) + os.urandom(fill + 2) + plain + b"\0" * 7
    out = b""
    prev_x = prev_c = b"\0" * 8
    for pos in range(0, len(data), 8):
        p = data[pos : pos + 8]
        x = bytes(i ^ j for i, j in zip(p, prev_c, strict=True))
        c = bytes(i ^ j for i, j in zip(_tea_encrypt_block(x, key), prev_x, strict=True))
        out += c
        prev_x, prev_c = x, c
    return out


# ---- protobuf ------------------------------------------------------------------------------
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _message(*fields: tuple[int, int | bytes | None]) -> bytes:
    return b"".join(_field(n, v) for n, v in fields if v is not None)


# ---- chunks ----------------------------------------------------------------------------------
def lz4_literals_only(raw: bytes) -> bytes:
    """A valid LZ4 block holding ``raw`` as one literal run (enough to test the decoder)."""
    length = len(raw)
    token = min(length, 15) << 4
    out = bytearray([token])
    if length >= 15:
        rest = length - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out) + raw


def lz4_with_match(prefix: bytes, repeat: int) -> tuple[bytes, bytes]:
    """An LZ4 block of ``prefix`` then ``repeat`` bytes copied from it (offset len(prefix)).

    LZ4 requires the last five bytes to be literals, so five literal bytes end the block.
    Returns ``(block, raw)``.
    """
    tail = b"ENDLZ"
    lit = len(prefix)
    match = repeat - 4  # LZ4 encodes match length minus 4
    token = (min(lit, 15) << 4) | min(match, 15)
    out = bytearray([token])
    if lit >= 15:
        rest = lit - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += prefix
    out += struct.pack("<H", len(prefix))
    if match >= 15:
        rest = match - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out.append(len(tail) << 4)
    out += tail
    raw = bytearray(prefix)
    for i in range(repeat):
        raw.append(raw[i % len(prefix)])
    raw += tail
    return bytes(out), bytes(raw)


def lzma2_chunk(raw: bytes) -> bytes:
    """Method 1: one dictionary-size byte, then a raw LZMA2 stream (as the publisher writes it)."""
    dict_byte = 16  # (2 | 0) << (16 // 2 + 11) = 1 MiB
    stream = lzma.compress(
        raw, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}]
    )
    return bytes([dict_byte]) + stream


def encode_chunk(raw: bytes, method: int) -> bytes:
    if method == 0:
        return raw
    if method == 1:
        return lzma2_chunk(raw)
    if method == 2:
        return lz4_literals_only(raw)
    if method == 3:
        return zstandard.ZstdCompressor().compress(raw)
    raise ValueError(method)


def md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # noqa: S324 - the publisher's own checksum


# ---- a whole repository ----------------------------------------------------------------------
class Repo:
    """Files in, manifest bytes and a CDN dict out.

    ``files`` maps a Windows-style path to its bytes (``None`` makes a directory entry);
    ``chunk_size`` splits files into several chunks; ``methods`` cycles compression methods.
    """

    def __init__(
        self,
        files: dict[str, bytes | None],
        *,
        repository_id: int = 50,
        chunk_size: int = 7,
        methods: tuple[int, ...] = (0, 1, 2, 3),
        cdn_root: str = "https://cloud.example.test/PC/prod/rid.50-r.x",
    ) -> None:
        self.repository_id = repository_id
        self.cdn_root = cdn_root
        self.cdn: dict[str, bytes] = {}
        entries = []
        paths = []
        turn = 0
        encoded: dict[bytes, tuple[bytes, int]] = {}
        for path, data in files.items():
            paths.append(path.encode())
            if data is None:
                entries.append(_message((1, 0), (2, 16), (3, 0)))
                continue
            chunks = []
            for pos in range(0, len(data), chunk_size):
                raw = data[pos : pos + chunk_size]
                # The CDN is content-addressed by the raw chunk: identical bytes must keep
                # the same wire object (and method), or the repository is not a real one.
                if raw in encoded:
                    wire, method = encoded[raw]
                else:
                    method = methods[turn % len(methods)]
                    turn += 1
                    wire = encode_chunk(raw, method)
                    encoded[raw] = (wire, method)
                url = f"{cdn_root}/chunksv2/{md5(raw).hex()[:2]}/{md5(raw).hex()}{len(raw):x}.wgc"
                self.cdn[url] = wire
                chunks.append(
                    _message(
                        (1, len(raw)), (2, len(wire)), (3, 0), (4, md5(wire)), (5, md5(raw)),
                        (6, pos), (7, method),
                    )
                )
            entries.append(
                _message((1, len(data)), (2, 32), (3, 0), (4, md5(data)), *[(5, c) for c in chunks])
            )
        names = b"".join(_field(1, p) for p in paths)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(names) + padder.finalize()
        enc = Cipher(algorithms.AES(bytes.fromhex(NAME_KEY)), modes.CBC(bytes(16))).encryptor()
        encrypt_info = enc.update(padded) + enc.finalize()
        body = _message(
            (1, repository_id), (2, 123456789), (6, b"Game"),
            (11, _message((1, chunk_size), (2, 0), (3, 0), (4, 0))),
            (12, encrypt_info),
            *[(13, e) for e in entries],
        )
        compressed = zstandard.ZstdCompressor().compress(body)
        header = _message((1, 2), (2, len(body)), (3, len(compressed)), (4, 3))
        self.manifest = qqtea_encrypt(_message((1, header), (2, compressed)))
        self.manifest_url = f"{cdn_root}/manifest/{repository_id}.bin"
        self.cdn[self.manifest_url] = self.manifest

    def version_info(self, **changes) -> dict:
        """A ``launcher_version_info`` record as the GetVersion API returns it."""
        repo_files = [
            {
                "cdn_root": self.cdn_root,
                "chunk_encrypt_flag": 0,
                "manifest_files": [
                    {
                        "file_url": self.manifest_url[len(self.cdn_root) :],
                        "manifest_size": len(self.manifest),
                        "manifest_md5": md5(self.manifest).hex(),
                    }
                ],
                "repository_id": self.repository_id,
            }
        ]
        access = [
            {
                "repository_id": self.repository_id,
                "manifest_encrytion_key": NAME_KEY,
                "manifest_encrytion_algorithm_id": 6,
            }
        ]
        info = {
            "game_id": "16701",
            "branch_id": "1",
            "version_id": "0.0.6.841",
            "version_name": "nikke_launcher_0.0.6.841",
            "cos_repo_files": json.dumps(repo_files),
            "cos_access_info": json.dumps(access),
        }
        info.update(changes)
        return info

    def fetch(self, url: str, size: int, md5_hex: str) -> bytes:
        """The fake CDN: exactly what a verified HTTPS fetch would return."""
        data = self.cdn[url]
        assert len(data) == size and hashlib.md5(data).hexdigest() == md5_hex  # noqa: S324
        return data
