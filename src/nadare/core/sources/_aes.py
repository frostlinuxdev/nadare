# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Dependency-free AES-128 CBC decryption, for small publisher manifests.

Nadare ships no crypto library, and some launchers wrap their file list in AES.
This is a straightforward table-free implementation: it is only meant for
manifests of a few hundred KB, never for game data.
"""

from __future__ import annotations

#: AES S-box (FIPS-197 figure 7).
_SBOX = bytes((
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5,
    0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0,
    0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC,
    0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A,
    0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0,
    0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B,
    0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85,
    0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17,
    0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88,
    0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C,
    0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9,
    0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6,
    0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E,
    0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94,
    0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68,
    0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
))

#: Inverse S-box, precomputed once at import.
_INV_SBOX = bytes(256)
_inv = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _inv[_v] = _i
_INV_SBOX = bytes(_inv)
del _i, _v, _inv

#: GF(2^8) multiply tables for the inverse MixColumns coefficients.
_MUL9 = bytes(256)
_MUL11 = bytes(256)
_MUL13 = bytes(256)
_MUL14 = bytes(256)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


_m9 = bytearray(256)
_m11 = bytearray(256)
_m13 = bytearray(256)
_m14 = bytearray(256)
for _x in range(256):
    _m9[_x] = _gmul(_x, 9)
    _m11[_x] = _gmul(_x, 11)
    _m13[_x] = _gmul(_x, 13)
    _m14[_x] = _gmul(_x, 14)
_MUL9, _MUL11, _MUL13, _MUL14 = bytes(_m9), bytes(_m11), bytes(_m13), bytes(_m14)
del _x, _m9, _m11, _m13, _m14


def _expand_key(key: bytes) -> list[bytes]:
    """Expand a 16-byte key into the 11 round keys used for decryption."""
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        w = words[i - 1]
        if i % 4 == 0:
            w = [_SBOX[w[1]] ^ rcon, _SBOX[w[2]], _SBOX[w[3]], _SBOX[w[0]]]
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], w)])
    return [bytes(b for word in words[r * 4:r * 4 + 4] for b in word) for r in range(11)]


def _decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    """Decrypt one 16-byte block with the AES-128 inverse cipher."""
    s = [a ^ b for a, b in zip(block, round_keys[10])]
    for rnd in range(9, -1, -1):
        # Inverse ShiftRows: row r is rotated right by r.
        s = [s[(i % 4) + 4 * ((i // 4 - i % 4) % 4)] for i in range(16)]
        s = [_INV_SBOX[b] for b in s]
        s = [a ^ b for a, b in zip(s, round_keys[rnd])]
        if rnd:
            # Inverse MixColumns.
            out = [0] * 16
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                out[c] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
                out[c + 1] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
                out[c + 2] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
                out[c + 3] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]
            s = out
    return bytes(s)


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt ``data`` (a multiple of 16 bytes) with AES-128-CBC. No padding is removed.

    Raises :class:`ValueError` for a key or IV that is not 16 bytes, or data whose
    length is not a multiple of 16.
    """
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    if len(iv) != 16:
        raise ValueError("AES-CBC IV must be 16 bytes")
    if len(data) % 16 != 0:
        raise ValueError("ciphertext length must be a multiple of 16")
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        block = data[off:off + 16]
        plain = _decrypt_block(block, round_keys)
        out.extend(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding; raise :class:`ValueError` when the padding is invalid."""
    if not data:
        raise ValueError("cannot unpad empty data")
    n = data[-1]
    if not 1 <= n <= 16 or n > len(data) or data[-n:] != bytes((n,)) * n:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-n]
