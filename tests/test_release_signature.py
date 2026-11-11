# SPDX-License-Identifier: GPL-3.0-or-later
"""The in-app updater checks the release signature against the pinned key (#1209 P0-1).

Before, it trusted a SHA-256 from the index (unsigned, TLS only): a changed index or a broken TLS
chain could serve any AppImage with a matching hash. Now the release's detached OpenPGP signature
(``<AppImage>.asc``, Ed25519, made by ``release/sign.sh``) must verify against the release key
built into Nadare, or nothing is installed. Signatures here are made with a throwaway key in the
same format gpg writes (v4, EdDSA, SHA-512), so the tests need no gpg.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nadare.core import releasesig

REPO = Path(__file__).resolve().parents[1]


def _subpacket(kind: int, body: bytes) -> bytes:
    return bytes([len(body) + 1, kind]) + body


def _mpi(value: bytes) -> bytes:
    stripped = value.lstrip(b"\0") or b"\0"
    return struct.pack(">H", int.from_bytes(stripped, "big").bit_length()) + stripped


def _armor(packet: bytes) -> str:
    body = base64.b64encode(packet).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    armored = "\n".join(lines)
    return f"-----BEGIN PGP SIGNATURE-----\n\n{armored}\n-----END PGP SIGNATURE-----\n"


def sign(
    data: bytes,
    key: Ed25519PrivateKey,
    fingerprint: bytes,
    *,
    hash_algo: int = 10,
    sigclass: int = 0x00,
    pubalgo: int = 22,
) -> str:
    """A detached v4 EdDSA signature over ``data``, shaped like gpg's."""
    hashed = _subpacket(33, b"\x04" + fingerprint) + _subpacket(2, struct.pack(">I", 1794312717))
    head = bytes([4, sigclass, pubalgo, hash_algo]) + struct.pack(">H", len(hashed)) + hashed
    trailer = b"\x04\xff" + struct.pack(">I", len(head))
    # The hash it names, so a refusal can only come from the algorithm check itself.
    h = {10: hashlib.sha512, 8: hashlib.sha256, 2: hashlib.sha1, 1: hashlib.md5}[hash_algo]
    digest = h(data + head + trailer).digest()
    raw = key.sign(digest)
    unhashed = _subpacket(16, fingerprint[-8:])
    body = head + struct.pack(">H", len(unhashed)) + unhashed + digest[:2]
    body += _mpi(raw[:32]) + _mpi(raw[32:])
    packet = bytes([0x80 | (2 << 2) | 1]) + struct.pack(">H", len(body)) + body
    return _armor(packet)


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()  # any 20 bytes: the test pins it as it likes
    return key, releasesig.PinnedKey(fingerprint=fingerprint.hex().upper(), ed25519=public)


def test_a_good_signature_verifies(tmp_path, keypair):
    key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"\x7fELF new nadare" * 1000)
    asc = sign(image.read_bytes(), key, bytes.fromhex(pinned.fingerprint))
    releasesig.verify_detached(image, asc, key=pinned)


def test_one_changed_byte_is_refused(tmp_path, keypair):
    key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"\x7fELF new nadare" * 1000)
    asc = sign(image.read_bytes(), key, bytes.fromhex(pinned.fingerprint))
    data = bytearray(image.read_bytes())
    data[500] ^= 1
    image.write_bytes(bytes(data))
    with pytest.raises(releasesig.SignatureError):
        releasesig.verify_detached(image, asc, key=pinned)


def test_a_signature_by_another_key_is_refused(tmp_path, keypair):
    _key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"payload")
    other = Ed25519PrivateKey.generate()
    asc = sign(image.read_bytes(), other, bytes.fromhex(pinned.fingerprint))  # claims our key
    with pytest.raises(releasesig.SignatureError):
        releasesig.verify_detached(image, asc, key=pinned)


def test_a_signature_naming_another_fingerprint_is_refused(tmp_path, keypair):
    key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"payload")
    asc = sign(image.read_bytes(), key, b"\x11" * 20)
    with pytest.raises(releasesig.SignatureError, match="key"):
        releasesig.verify_detached(image, asc, key=pinned)


GARBLED = "-----BEGIN PGP SIGNATURE-----\n\n!!!\n-----END PGP SIGNATURE-----\n"


@pytest.mark.parametrize("bad", ["", "not a signature", GARBLED])
def test_a_missing_or_garbled_signature_is_refused(tmp_path, keypair, bad):
    _key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"payload")
    with pytest.raises(releasesig.SignatureError):
        releasesig.verify_detached(image, bad, key=pinned)


@pytest.mark.parametrize("change", [{"hash_algo": 2}, {"sigclass": 0x01}, {"pubalgo": 1}])
def test_only_the_release_signature_shape_is_accepted(tmp_path, keypair, change):
    """SHA-1, a text-mode signature or another algorithm are refused, never guessed at."""
    key, pinned = keypair
    image = tmp_path / "Nadare.AppImage"
    image.write_bytes(b"payload")
    asc = sign(image.read_bytes(), key, bytes.fromhex(pinned.fingerprint), **change)
    with pytest.raises(releasesig.SignatureError):
        releasesig.verify_detached(image, asc, key=pinned)


def test_the_pinned_key_is_the_published_release_key():
    """The pin must be the key in packaging/signing (the one release/sign.sh signs with)."""
    asc = (REPO / "packaging" / "signing" / "nadare-release-signing.asc").read_text()
    fingerprint, raw = releasesig.read_public_key(asc)
    assert fingerprint == releasesig.RELEASE_KEY.fingerprint
    assert fingerprint == "0F536F244C481212A3095B521E05855E99D918D5"
    assert raw == releasesig.RELEASE_KEY.ed25519
