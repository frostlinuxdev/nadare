"""scripts/sign_appimage.py: embed and check an AppImage signature after the build.

The release key must never enter the build container, so the AppImage is built unsigned
(with update information) and signed afterwards on the signing host. The embedded
signature must be exactly what ``appimagetool --sign`` 1.9.1 writes and what
AppImageUpdate checks: a detached, armored, text-mode gpg signature of the lowercase hex
SHA-256 of the file with ``.sha256_sig`` and ``.sig_key`` read as zeros, the signature in
``.sha256_sig`` and the armored public key in ``.sig_key``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sign_appimage.py"

# CI installs gnupg; there a missing gpg is a broken runner, not a reason to skip.
pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None and not os.environ.get("CI"), reason="gpg is not installed"
)


def _load():
    spec = importlib.util.spec_from_file_location("sign_appimage", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SECTIONS = ((".upd_info", 1024), (".sha256_sig", 1024), (".sig_key", 8192))


def _fake_appimage(path: Path, payload: bytes = b"squashfs payload " * 3000) -> Path:
    """A minimal ELF64 with the type-2 runtime's sections, followed by a payload."""
    names = b"\0" + b"\0".join(n.encode() for n, _ in SECTIONS) + b"\0.shstrtab\0"
    header_size = 64
    data_offset = header_size
    blobs = []
    offsets = []
    for _, size in SECTIONS:
        offsets.append(data_offset)
        blobs.append(b"\0" * size)
        data_offset += size
    strtab_offset = data_offset
    data_offset += len(names)
    shoff = data_offset
    shnum = len(SECTIONS) + 2  # null section, our sections, .shstrtab
    out = bytearray(b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9)
    out += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, shnum, shnum - 1)
    assert len(out) == header_size
    for blob in blobs:
        out += blob
    out += names
    headers = [b"\0" * 64]
    name_pos = 1
    for (name, size), offset in zip(SECTIONS, offsets, strict=True):
        headers.append(struct.pack("<IIQQQQIIQQ", name_pos, 1, 0, 0, offset, size, 0, 0, 1, 0))
        name_pos += len(name) + 1
    headers.append(
        struct.pack("<IIQQQQIIQQ", name_pos, 3, 0, 0, strtab_offset, len(names), 0, 0, 1, 0)
    )
    out += b"".join(headers)
    out += payload
    path.write_bytes(bytes(out))
    return path


@pytest.fixture(scope="module")
def gnupghome(tmp_path_factory):
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    params = home / "params"
    params.write_text(
        "Key-Type: eddsa\nKey-Curve: ed25519\nKey-Usage: sign\n"
        "Name-Real: Nadare test signing\nExpire-Date: 0\n%no-protection\n%commit\n"
    )
    env = dict(os.environ, GNUPGHOME=str(home))
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--generate-key", str(params)],
        env=env,
        check=True,
        capture_output=True,
    )
    listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    yield home, fpr
    subprocess.run(["gpgconf", "--kill", "all"], env=env, capture_output=True)


def _zeroed_digest(path: Path, mod) -> str:
    data = bytearray(path.read_bytes())
    for name in (".sha256_sig", ".sig_key"):
        offset, size = mod.section(path, name)
        data[offset : offset + size] = b"\0" * size
    return hashlib.sha256(bytes(data)).hexdigest()


def test_sections_are_found_where_the_elf_says(tmp_path):
    mod = _load()
    image = _fake_appimage(tmp_path / "a.AppImage")
    assert mod.section(image, ".upd_info") == (64, 1024)
    assert mod.section(image, ".sha256_sig") == (64 + 1024, 1024)
    assert mod.section(image, ".sig_key") == (64 + 2048, 8192)


def test_sign_embeds_a_signature_of_the_zeroed_digest_and_the_public_key(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    before = _zeroed_digest(image, mod)
    mod.sign(image, fpr, gnupghome=home)

    sig = mod.read_section(image, ".sha256_sig")
    key = mod.read_section(image, ".sig_key")
    assert sig.startswith(b"-----BEGIN PGP SIGNATURE-----")
    assert key.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
    # Signing changes only the two sections, so the zeroed digest is unchanged.
    assert _zeroed_digest(image, mod) == before

    # Independent check with plain gpg: the signature is over the hex digest, text mode.
    check = tmp_path / "check"
    check.mkdir(mode=0o700)
    env = dict(os.environ, GNUPGHOME=str(check))
    (tmp_path / "key.asc").write_bytes(key)
    subprocess.run(
        ["gpg", "--batch", "--import", str(tmp_path / "key.asc")],
        env=env,
        check=True,
        capture_output=True,
    )
    (tmp_path / "digest").write_text(before)
    (tmp_path / "digest.asc").write_bytes(sig)
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--status-fd",
            "1",
            "--verify",
            str(tmp_path / "digest.asc"),
            str(tmp_path / "digest"),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert f"VALIDSIG {fpr}" in result.stdout
    assert "SIG_CREATED" not in result.stdout
    assert " 01 " in next(line for line in result.stdout.splitlines() if "VALIDSIG" in line), (
        "a text-mode (class 01) signature, as appimagetool makes"
    )
    subprocess.run(["gpgconf", "--kill", "all"], env=env, capture_output=True)


def test_verify_accepts_a_signed_image_and_names_the_key(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    mod.sign(image, fpr, gnupghome=home)
    assert mod.verify(image, expect_fingerprint=fpr) == fpr


def test_verify_rejects_a_changed_payload(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    mod.sign(image, fpr, gnupghome=home)
    data = bytearray(image.read_bytes())
    data[-10] ^= 0xFF
    image.write_bytes(bytes(data))
    with pytest.raises(mod.SignError, match="does not verify"):
        mod.verify(image)


def test_verify_rejects_another_key(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    mod.sign(image, fpr, gnupghome=home)
    with pytest.raises(mod.SignError, match="not the expected key"):
        mod.verify(image, expect_fingerprint="0" * 40)


def test_verify_rejects_an_unsigned_image(tmp_path):
    mod = _load()
    image = _fake_appimage(tmp_path / "a.AppImage")
    with pytest.raises(mod.SignError, match="not signed"):
        mod.verify(image)


def test_sign_refuses_an_image_that_is_already_signed(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    mod.sign(image, fpr, gnupghome=home)
    with pytest.raises(mod.SignError, match="already signed"):
        mod.sign(image, fpr, gnupghome=home)


def test_same_content_ignores_only_the_signature_sections(tmp_path, gnupghome):
    mod = _load()
    home, fpr = gnupghome
    original = _fake_appimage(tmp_path / "a.AppImage")
    signed = tmp_path / "b.AppImage"
    shutil.copy(original, signed)
    mod.sign(signed, fpr, gnupghome=home)
    assert mod.same_content(original, signed)

    other = tmp_path / "c.AppImage"
    shutil.copy(original, other)
    data = bytearray(other.read_bytes())
    offset, _ = mod.section(other, ".upd_info")
    data[offset] = ord("x")  # update information is NOT a signature section
    other.write_bytes(bytes(data))
    assert not mod.same_content(original, other)


def test_command_line_sign_then_verify(tmp_path, gnupghome):
    home, fpr = gnupghome
    image = _fake_appimage(tmp_path / "a.AppImage")
    env = dict(os.environ, GNUPGHOME=str(home))
    run = [sys.executable, "-I", str(_SCRIPT)]
    signed = subprocess.run(
        [*run, "sign", str(image), "--key", fpr], env=env, capture_output=True, text=True
    )
    assert signed.returncode == 0, signed.stderr
    checked = subprocess.run(
        [*run, "verify", str(image), "--fingerprint", fpr], capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stderr
    assert fpr in checked.stdout
    image.write_bytes(image.read_bytes()[:-1] + b"!")
    bad = subprocess.run([*run, "verify", str(image)], capture_output=True, text=True)
    assert bad.returncode != 0


def test_the_published_key_file_matches_the_fingerprint_in_the_docs(tmp_path):
    key = _REPO_ROOT / "packaging" / "signing" / "nadare-release-signing.asc"
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--show-keys", str(key)],
        env=dict(os.environ, GNUPGHOME=str(home)),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fprs = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
    assert len(fprs) == 1, "one key, no subkeys"
    assert "private" not in key.read_text().lower()
    spaced = " ".join(fprs[0][i : i + 4] for i in range(0, 40, 4)).replace(
        fprs[0][16:20] + " ", fprs[0][16:20] + "  ", 1
    )
    for doc in ("docs/PACKAGING.md", "docs/INSTALL.md"):
        assert spaced in (_REPO_ROOT / doc).read_text(encoding="utf-8"), doc
