# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Check a release's detached OpenPGP signature against the release key built into Nadare.

Releases are signed by ``release/sign.sh`` with an Ed25519 key (gpg: v4 signature, EdDSA, SHA-512,
binary document). The in-app updater downloads ``<AppImage>.asc`` and calls
:func:`verify_detached` before it replaces anything (#1209 P0-1): the index's SHA-256 alone came
over TLS only and was trusted as is.

Only that exact shape is accepted; anything else is refused, never guessed at. No gpg is run on
the user's machine: the packet is read here and the Ed25519 check is :mod:`cryptography`'s.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class SignatureError(Exception):
    """The signature is missing, malformed, by another key, or does not match the file."""


@dataclass(frozen=True)
class PinnedKey:
    #: The v4 fingerprint, 40 upper-case hex digits.
    fingerprint: str
    #: The raw 32-byte Ed25519 public key.
    ed25519: bytes


#: The AppImage release key (DECISIONS 2026-11-10). Public copy: packaging/signing/
#: nadare-release-signing.asc; tests/test_release_signature.py checks the two agree.
RELEASE_KEY = PinnedKey(
    fingerprint="0F536F244C481212A3095B521E05855E99D918D5",
    ed25519=bytes.fromhex("bbd9b6ded191a94722e129dc5ac5251e09b7d1da742c82bd012caf3e86058010"),
)

_PUBALGO_EDDSA = 22  # EdDSA "legacy" (the algorithm gpg uses for Ed25519 v4 keys)
_HASHES = {10: hashlib.sha512, 8: hashlib.sha256}  # SHA-1 and MD5 are never accepted
_ED25519_OID = bytes.fromhex("2B06010401DA470F01")
_ARMOR = re.compile(
    r"-----BEGIN PGP (?P<kind>SIGNATURE|PUBLIC KEY BLOCK)-----\r?\n(?P<body>.*?)"
    r"-----END PGP (?P=kind)-----",
    re.S,
)
_MAX_SIGNATURE = 16 * 1024


def _dearmor(text: str, kind: str) -> bytes:
    match = _ARMOR.search(text or "")
    if match is None or match.group("kind") != kind:
        raise SignatureError(f"no PGP {kind.lower()} found")
    lines = match.group("body").splitlines()
    while lines and lines[0].strip() and ":" in lines[0]:  # armor headers (Version: ...)
        lines.pop(0)
    data = "".join(line.strip() for line in lines if line.strip() and not line.startswith("="))
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("the signature is not valid base64") from exc


def _packets(data: bytes):
    """``(tag, body)`` for each packet (old and new header formats)."""
    pos = 0
    while pos < len(data):
        head = data[pos]
        if not head & 0x80:
            raise SignatureError("not an OpenPGP packet")
        if head & 0x40:  # new format
            tag = head & 0x3F
            first = data[pos + 1]
            if first < 192:
                length, pos = first, pos + 2
            elif first < 224:
                length, pos = ((first - 192) << 8) + data[pos + 2] + 192, pos + 3
            elif first == 255:
                length, pos = int.from_bytes(data[pos + 2 : pos + 6], "big"), pos + 6
            else:
                raise SignatureError("partial-length packets are not used by Nadare releases")
        else:
            tag = (head >> 2) & 0x0F
            kind = head & 0x03
            size = {0: 1, 1: 2, 2: 4}.get(kind)
            if size is None:
                raise SignatureError("indeterminate-length packets are not accepted")
            length = int.from_bytes(data[pos + 1 : pos + 1 + size], "big")
            pos += 1 + size
        body = data[pos : pos + length]
        if len(body) != length:
            raise SignatureError("the packet is cut short")
        yield tag, body
        pos += length


def _mpi(body: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 2 > len(body):
        raise SignatureError("the packet is cut short")
    bits = int.from_bytes(body[pos : pos + 2], "big")
    size = (bits + 7) // 8
    value = body[pos + 2 : pos + 2 + size]
    if len(value) != size:
        raise SignatureError("the packet is cut short")
    return value, pos + 2 + size


def _subpackets(area: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(area):
        first = area[pos]
        if first < 192:
            length, pos = first, pos + 1
        elif first < 255:
            length, pos = ((first - 192) << 8) + area[pos + 1] + 192, pos + 2
        else:
            length, pos = int.from_bytes(area[pos + 1 : pos + 5], "big"), pos + 5
        if length < 1 or pos + length > len(area):
            raise SignatureError("a signature subpacket is damaged")
        out.append((area[pos] & 0x7F, area[pos + 1 : pos + length]))
        pos += length
    return out


def verify_detached(path: str | Path, signature: str, *, key: PinnedKey = RELEASE_KEY) -> None:
    """Return if ``signature`` (an armored ``.asc``) is ``key``'s over ``path``; else raise."""
    if not signature or len(signature) > _MAX_SIGNATURE:
        raise SignatureError("the release signature is missing")
    packets = [(tag, body) for tag, body in _packets(_dearmor(signature, "SIGNATURE")) if tag == 2]
    if len(packets) != 1:
        raise SignatureError("expected exactly one signature")
    body = packets[0][1]
    if len(body) < 6 or body[0] != 4:
        raise SignatureError("only version 4 signatures are accepted")
    sigclass, pubalgo, hashalgo = body[1], body[2], body[3]
    if sigclass != 0x00 or pubalgo != _PUBALGO_EDDSA or hashalgo not in _HASHES:
        raise SignatureError("this is not a Nadare release signature (type, algorithm or hash)")
    hashed_len = int.from_bytes(body[4:6], "big")
    head = body[: 6 + hashed_len]
    if len(head) != 6 + hashed_len:
        raise SignatureError("the packet is cut short")
    pos = 6 + hashed_len
    unhashed_len = int.from_bytes(body[pos : pos + 2], "big")
    pos += 2 + unhashed_len + 2  # unhashed area, then the two "left 16 bits" of the digest
    r, pos = _mpi(body, pos)
    s, pos = _mpi(body, pos)
    if len(r) > 32 or len(s) > 32:
        raise SignatureError("the signature values are the wrong size")
    issuers = [
        value[1:].hex().upper()
        for kind, value in _subpackets(head[6:])
        if kind == 33 and value[:1] == b"\x04"
    ]
    if issuers and issuers != [key.fingerprint]:
        raise SignatureError("signed with another key than Nadare's release key")
    digest = _HASHES[hashalgo]()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    digest.update(head)
    digest.update(b"\x04\xff" + len(head).to_bytes(4, "big"))
    raw = r.rjust(32, b"\0") + s.rjust(32, b"\0")
    try:
        Ed25519PublicKey.from_public_bytes(key.ed25519).verify(raw, digest.digest())
    except InvalidSignature:
        raise SignatureError("the signature does not match this file") from None


def read_public_key(armored: str) -> tuple[str, bytes]:
    """``(fingerprint, raw Ed25519 key)`` of the primary key in an armored public key block."""
    for tag, body in _packets(_dearmor(armored, "PUBLIC KEY BLOCK")):
        if tag != 6:
            continue
        if body[0] != 4 or body[5] != _PUBALGO_EDDSA:
            raise SignatureError("not a v4 EdDSA public key")
        oid_len = body[6]
        if body[7 : 7 + oid_len] != _ED25519_OID:
            raise SignatureError("not an Ed25519 key")
        point, end = _mpi(body, 7 + oid_len)
        if len(point) != 33 or point[0] != 0x40:
            raise SignatureError("unexpected Ed25519 point encoding")
        packet = body[:end]
        fingerprint = hashlib.sha1(b"\x99" + len(packet).to_bytes(2, "big") + packet).hexdigest()  # noqa: S324 - the v4 fingerprint is defined as SHA-1
        return fingerprint.upper(), point[1:]
    raise SignatureError("no public key in the block")


__all__ = ["RELEASE_KEY", "PinnedKey", "SignatureError", "read_public_key", "verify_detached"]
