#!/usr/bin/env python3
"""Embed and check an AppImage signature after the build.

The release key stays on the signing host and never enters the build container, so the
AppImage is built unsigned and signed here afterwards. The result is what
``appimagetool --sign`` (1.9.1) writes and what AppImageUpdate checks:

* the digest is the lowercase hex SHA-256 of the file with the ``.sha256_sig`` and
  ``.sig_key`` sections read as zeros;
* ``.sha256_sig`` holds a detached, armored, text-mode gpg signature of that hex string;
* ``.sig_key`` holds the signer's armored public key.

Only those two sections change, so the image tested before signing is the image shipped
(``same-content`` proves it byte for byte).

Usage:
  sign_appimage.py sign APPIMAGE --key FINGERPRINT     (uses $GNUPGHOME)
  sign_appimage.py verify APPIMAGE [--fingerprint FINGERPRINT]
  sign_appimage.py same-content UNSIGNED SIGNED
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SIGNATURE_SECTION = ".sha256_sig"
KEY_SECTION = ".sig_key"
_CHUNK = 1 << 20


class SignError(Exception):
    """The image cannot be signed or does not verify."""


def _sections(path: Path) -> dict[str, tuple[int, int]]:
    """Name -> (offset, size) for every section of a little-endian ELF64 file."""
    with open(path, "rb") as fh:
        header = fh.read(64)
        if header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
            raise SignError(f"{path} is not a little-endian ELF64 file (an AppImage type 2)")
        shoff = struct.unpack_from("<Q", header, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", header, 0x3A)
        if shentsize != 64 or not shnum or shstrndx >= shnum:
            raise SignError(f"{path}: unexpected ELF section table")
        fh.seek(shoff)
        table = fh.read(shentsize * shnum)
        entries = []
        for i in range(shnum):
            name, _type, _flags, _addr, offset, size = struct.unpack_from(
                "<IIQQQQ", table, i * shentsize
            )
            entries.append((name, offset, size))
        _, str_offset, str_size = entries[shstrndx]
        fh.seek(str_offset)
        strings = fh.read(str_size)
    out = {}
    for name, offset, size in entries:
        end = strings.find(b"\0", name)
        out[strings[name:end].decode("ascii", "replace")] = (offset, size)
    return out


def section(path: Path, name: str) -> tuple[int, int]:
    found = _sections(Path(path)).get(name)
    if not found or not found[1]:
        raise SignError(f"{path} has no {name} section")
    return found


def read_section(path: Path, name: str) -> bytes:
    offset, size = section(path, name)
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size).rstrip(b"\0")


def _digest(path: Path) -> str:
    """Hex SHA-256 with the signature and key sections read as zeros (AppImageUpdate's way)."""
    skip = sorted(section(path, n) for n in (SIGNATURE_SECTION, KEY_SECTION))
    digest = hashlib.sha256()
    pos = 0
    with open(path, "rb") as fh:
        for offset, size in skip:
            _feed(fh, digest, offset - pos)
            digest.update(b"\0" * size)
            fh.seek(size, os.SEEK_CUR)
            pos = offset + size
        _feed(fh, digest, None)
    return digest.hexdigest()


def _feed(fh, digest, count: int | None) -> None:
    while count is None or count > 0:
        block = fh.read(_CHUNK if count is None else min(_CHUNK, count))
        if not block:
            if count:
                raise SignError("file ended inside a section")
            return
        digest.update(block)
        if count is not None:
            count -= len(block)


def _gpg(args: list[str], *, gnupghome: Path | None, data: bytes | None = None) -> bytes:
    env = dict(os.environ)
    if gnupghome is not None:
        env["GNUPGHOME"] = str(gnupghome)
    result = subprocess.run(
        ["gpg", "--batch", "--no-tty", *args], input=data, capture_output=True, env=env
    )
    if result.returncode != 0:
        raise SignError("gpg failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def _write(path: Path, name: str, data: bytes) -> None:
    offset, size = section(path, name)
    if len(data) > size:
        raise SignError(f"{name} holds {size} bytes; the data is {len(data)}")
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(data + b"\0" * (size - len(data)))


def sign(path: Path, key: str, *, gnupghome: Path | None = None) -> str:
    """Sign ``path`` in place with ``key``; returns the signer's fingerprint."""
    path = Path(path)
    if read_section(path, SIGNATURE_SECTION) or read_section(path, KEY_SECTION):
        raise SignError(f"{path} is already signed")
    hexdigest = _digest(path).encode()
    signature = _gpg(
        ["--armor", "--textmode", "--detach-sign", "--local-user", key],
        gnupghome=gnupghome,
        data=hexdigest,
    )
    public = _gpg(["--armor", "--export", key], gnupghome=gnupghome)
    if not public:
        raise SignError(f"no public key for {key}")
    _write(path, SIGNATURE_SECTION, signature)
    _write(path, KEY_SECTION, public)
    return verify(path, expect_fingerprint=key if len(key) == 40 else None)


def verify(path: Path, *, expect_fingerprint: str | None = None) -> str:
    """Check the embedded signature against the embedded key; returns the fingerprint."""
    path = Path(path)
    signature = read_section(path, SIGNATURE_SECTION)
    public = read_section(path, KEY_SECTION)
    if not signature or not public:
        raise SignError(f"{path} is not signed")
    hexdigest = _digest(path)
    with tempfile.TemporaryDirectory(prefix="nadare-verify-") as tmp:
        home = Path(tmp) / "gnupg"
        home.mkdir(mode=0o700)
        try:
            _gpg(["--import"], gnupghome=home, data=public)
            (Path(tmp) / "digest").write_text(hexdigest)
            (Path(tmp) / "digest.asc").write_bytes(signature)
            env = dict(os.environ, GNUPGHOME=str(home))
            result = subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--no-tty",
                    "--status-fd",
                    "1",
                    "--verify",
                    str(Path(tmp) / "digest.asc"),
                    str(Path(tmp) / "digest"),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            subprocess.run(
                ["gpgconf", "--kill", "all"],
                env=dict(os.environ, GNUPGHOME=str(home)),
                capture_output=True,
            )
    valid = [line.split() for line in result.stdout.splitlines() if " VALIDSIG " in f" {line}"]
    if result.returncode != 0 or not valid:
        raise SignError(f"{path}: the embedded signature does not verify")
    fingerprint = valid[0][2]
    primary = valid[0][-1]
    if expect_fingerprint and expect_fingerprint.upper() not in (fingerprint, primary):
        raise SignError(f"{path}: signed by {primary}, not the expected key {expect_fingerprint}")
    return primary


def same_content(unsigned: Path, signed: Path) -> bool:
    """True when the files differ at most inside the signature and key sections."""
    unsigned, signed = Path(unsigned), Path(signed)
    if unsigned.stat().st_size != signed.stat().st_size:
        return False
    if section(unsigned, SIGNATURE_SECTION) != section(signed, SIGNATURE_SECTION):
        return False
    if section(unsigned, KEY_SECTION) != section(signed, KEY_SECTION):
        return False
    return _digest(unsigned) == _digest(signed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_sign = sub.add_parser("sign")
    p_sign.add_argument("appimage", type=Path)
    p_sign.add_argument("--key", required=True, help="signing key fingerprint")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("appimage", type=Path)
    p_verify.add_argument("--fingerprint")
    p_same = sub.add_parser("same-content")
    p_same.add_argument("unsigned", type=Path)
    p_same.add_argument("signed", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sign":
            print(f"SIGNED {sign(args.appimage, args.key)} {args.appimage}")
        elif args.command == "verify":
            print(f"VALID {verify(args.appimage, expect_fingerprint=args.fingerprint)}")
        else:
            if not same_content(args.unsigned, args.signed):
                print("DIFFERENT: the files differ outside the signature sections")
                return 1
            print("SAME-CONTENT")
    except SignError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
