# SPDX-License-Identifier: GPL-3.0-or-later
"""Known-answer tests for the dependency-free AES-128-CBC decryptor."""

from __future__ import annotations

import pytest

from nadare.core.sources._aes import aes128_cbc_decrypt, pkcs7_unpad

h = bytes.fromhex


def test_fips197_single_block():
    # FIPS-197 appendix C.1 (CBC with a zero IV is ECB for one block)
    key = h("000102030405060708090a0b0c0d0e0f")
    ct = h("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert aes128_cbc_decrypt(key, bytes(16), ct) == h("00112233445566778899aabbccddeeff")


def test_nist_sp800_38a_cbc_aes128_four_blocks():
    key = h("2b7e151628aed2a6abf7158809cf4f3c")
    iv = h("000102030405060708090a0b0c0d0e0f")
    ct = h(
        "7649abac8119b246cee98e9b12e9197d"
        "5086cb9b507219ee95db113a917678b2"
        "73bed6b8e3c1743b7116e69e22229516"
        "3ff1caa1681fac09120eca307586e1a7"
    )
    pt = h(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710"
    )
    assert aes128_cbc_decrypt(key, iv, ct) == pt


def test_empty_input_is_empty_output():
    assert aes128_cbc_decrypt(bytes(16), bytes(16), b"") == b""


@pytest.mark.parametrize("key,iv,data", [(b"k" * 15, bytes(16), bytes(16)),
                                         (bytes(16), b"i" * 17, bytes(16)),
                                         (bytes(16), bytes(16), b"x" * 17)])
def test_bad_sizes_raise_value_error(key, iv, data):
    with pytest.raises(ValueError):
        aes128_cbc_decrypt(key, iv, data)


def test_pkcs7_unpad():
    assert pkcs7_unpad(b"hello" + bytes([11]) * 11) == b"hello"
    assert pkcs7_unpad(bytes([16]) * 16) == b""
    for bad in (b"", b"abc", b"x" * 15 + b"\x00", b"x" * 15 + b"\x11", b"x" * 14 + b"\x01\x02"):
        with pytest.raises(ValueError):
            pkcs7_unpad(bad)
