# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Tencent's Jupiter "rail_setup" launcher repositories.

A rail_setup repository is a QQTEA-wrapped manifest (zstd-compressed protobuf) plus
content-addressed chunks on the launcher CDN.  The manifest lists every file with its
MD5 and the chunks that make it up; the file names themselves are stored in an
AES-128-CBC encrypted ``EncryptInfo`` message.  Nothing the publisher ships is executed:
Nadare reads the public data itself, verifies every manifest and chunk against its MD5,
and only then writes the files below the chosen destination.

Only the standard library, :mod:`zstandard` and :mod:`cryptography` are used here.
"""

from __future__ import annotations

import hashlib
import lzma
import os
import stat
import struct
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .events import Cancel, Cancelled, Event

__all__ = [
    "Chunk",
    "Entry",
    "InstallResult",
    "PI_KEY",
    "RailError",
    "Repository",
    "chunk_url",
    "decode_chunk",
    "install",
    "lz4_block_decompress",
    "parse_manifest",
    "qqtea_decrypt",
    "repositories",
]

#: The public "pi" key Tencent uses to wrap rail_setup manifests.
PI_KEY = b"3.14159265358979"

_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9
_MAX_MANIFEST_INPUT = 32 * 1024 * 1024
_MAX_MANIFEST_BODY = 64 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_CHUNK_RAW = 8 * 1024 * 1024
_MAX_CHUNK_WIRE = 32 * 1024 * 1024
_PARTIAL_DIR = ".nadare-partial"
#: A temporary file in ``_PARTIAL_DIR`` untouched this long is from a run that was killed (#991);
#: a live download rewrites its file every chunk.
_STALE_PARTIAL_SECONDS = 3600


class RailError(Exception):
    """A malformed repository, manifest, chunk or local install."""


# ---------------------------------------------------------------------------
# QQTEA
# ---------------------------------------------------------------------------
def _tea_decrypt_block(block: bytes, key: bytes) -> bytes:
    try:
        a, b = struct.unpack(">2I", block)
        k = struct.unpack(">4I", key)
    except (ValueError, TypeError, struct.error) as exc:
        raise RailError("invalid QQTEA block or key") from exc
    total = 0
    for _ in range(16):
        total = (total + _DELTA) & _MASK
    s = total
    for _ in range(16):
        b = (
            b
            - ((((a << 4) + k[2]) & _MASK) ^ ((a + s) & _MASK) ^ (((a >> 5) + k[3]) & _MASK))
        ) & _MASK
        a = (
            a
            - ((((b << 4) + k[0]) & _MASK) ^ ((b + s) & _MASK) ^ (((b >> 5) + k[1]) & _MASK))
        ) & _MASK
        s = (s - _DELTA) & _MASK
    return struct.pack(">2I", a, b)


def qqtea_decrypt(data: bytes, key: bytes = PI_KEY) -> bytes:
    """Undo Tencent's modified TEA (QQTEA) wrapper, including its padding check."""
    if not isinstance(data, (bytes, bytearray)) or not isinstance(key, (bytes, bytearray)):
        raise RailError("QQTEA data and key must be bytes")
    data = bytes(data)
    key = bytes(key)
    if len(key) != 16 or len(data) < 16 or len(data) % 8:
        raise RailError("invalid QQTEA key or ciphertext length")
    prev_x = prev_c = b"\0" * 8
    result = bytearray()
    for pos in range(0, len(data), 8):
        c = data[pos : pos + 8]
        x = _tea_decrypt_block(bytes(a ^ b for a, b in zip(c, prev_x, strict=True)), key)
        result += bytes(a ^ b for a, b in zip(x, prev_c, strict=True))
        prev_x, prev_c = x, c
    pad = result[0] & 7
    if len(result) < pad + 10 or result[-7:] != b"\0" * 7:
        raise RailError("invalid QQTEA padding or incorrect key")
    return bytes(result[pad + 3 : -7])


# ---------------------------------------------------------------------------
# LZ4 block format
# ---------------------------------------------------------------------------
def lz4_block_decompress(data: bytes, size: int) -> bytes:
    """Decode a raw LZ4 block whose expected uncompressed length is ``size``."""
    if size < 0:
        raise RailError("invalid LZ4 output size")
    out = bytearray()
    pos = 0
    end = len(data)
    while pos < end:
        token = data[pos]
        pos += 1
        literal = token >> 4
        if literal == 15:
            while True:
                if pos >= end:
                    raise RailError("truncated LZ4 literal-length extension")
                byte = data[pos]
                pos += 1
                literal += byte
                if byte != 0xFF:
                    break
        if pos + literal > end:
            raise RailError("LZ4 literal run past the end of the block")
        if len(out) + literal > size:
            raise RailError("LZ4 output longer than declared")
        out += data[pos : pos + literal]
        pos += literal
        if pos == end:
            break
        if pos + 2 > end:
            raise RailError("truncated LZ4 match offset")
        offset = data[pos] | (data[pos + 1] << 8)
        pos += 2
        if offset == 0:
            raise RailError("LZ4 match offset is zero")
        if offset > len(out):
            raise RailError("LZ4 match offset is past the output so far")
        match = token & 0x0F
        if match == 15:
            while True:
                if pos >= end:
                    raise RailError("truncated LZ4 match-length extension")
                byte = data[pos]
                pos += 1
                match += byte
                if byte != 0xFF:
                    break
        match += 4
        if len(out) + match > size:
            raise RailError("LZ4 output longer than declared")
        start = len(out) - offset
        if offset >= match:
            out += out[start : start + match]
        else:
            for index in range(match):
                out.append(out[start + index])
    if len(out) != size:
        raise RailError(f"LZ4 output is {len(out)} bytes, expected {size}")
    return bytes(out)


# ---------------------------------------------------------------------------
# The protobuf subset used by rail_setup
# ---------------------------------------------------------------------------
def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise RailError("truncated protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise RailError("protobuf varint is too long")


def _read_fields(data: bytes, limit: int | None = None) -> list[tuple[int, int, Any]]:
    """Decode one message into ``(field_number, wire_type, value)`` triples.

    ``limit`` caps the number of fields: a 64 MiB body of two-byte fields would otherwise
    become tens of millions of tuples before any count check could run.
    """
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        if limit is not None and len(fields) >= limit:
            raise RailError("protobuf message has too many fields")
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 0x07
        if number == 0:
            raise RailError("protobuf field number 0")
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data):
                raise RailError("truncated protobuf fixed64")
            value = data[pos : pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise RailError("truncated protobuf field")
            value = data[pos : pos + length]
            pos += length
        elif wire == 5:
            if pos + 4 > len(data):
                raise RailError("truncated protobuf fixed32")
            value = data[pos : pos + 4]
            pos += 4
        else:
            raise RailError(f"unsupported protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


class _Fields:
    """The last value of each singular field, with proto3 defaults for missing ones."""

    def __init__(self, data: bytes, limit: int | None = None) -> None:
        self.fields = _read_fields(data, limit)

    def integer(self, number: int, default: int = 0) -> int:
        for field, wire, value in reversed(self.fields):
            if field == number:
                if wire != 0:
                    raise RailError(f"protobuf field {number} is not a varint")
                return value
        return default

    def blob(self, number: int, default: bytes = b"") -> bytes:
        for field, wire, value in reversed(self.fields):
            if field == number:
                if wire != 2:
                    raise RailError(f"protobuf field {number} is not bytes")
                return value
        return default

    def messages(self, number: int) -> list[_Fields]:
        out = []
        for field, wire, value in self.fields:
            if field == number:
                if wire != 2:
                    raise RailError(f"protobuf field {number} is not a message")
                out.append(_Fields(value))
        return out

    def blobs(self, number: int) -> list[bytes]:
        out = []
        for field, wire, value in self.fields:
            if field == number:
                if wire != 2:
                    raise RailError(f"protobuf field {number} is not bytes")
                out.append(value)
        return out


def _zstd_decompress(data: bytes, size: int, label: str) -> bytes:
    try:
        out = zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    except Exception as exc:
        raise RailError(f"invalid zstd {label}") from exc
    return out


# ---------------------------------------------------------------------------
# Repositories and manifests
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    size: int
    compressed_size: int
    compressed_md5: bytes
    md5: bytes
    method: int


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    md5: bytes
    is_dir: bool
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class Repository:
    repository_id: int
    cdn_root: str
    manifest_url: str
    manifest_size: int
    manifest_md5: str
    key_hex: str
    algorithm: int


def _nested_json(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        raise RailError("repository metadata is not JSON text")
    import json

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        try:
            return json.loads(value.replace('\\"', '"'))
        except json.JSONDecodeError as exc:
            raise RailError("invalid repository metadata JSON") from exc


def _repository_list(info: dict[str, Any]) -> tuple[list[Repository], dict[int, dict[str, Any]]]:
    try:
        repo_records = _nested_json(info["cos_repo_files"])
        access_records = _nested_json(info["cos_access_info"])
    except (KeyError, TypeError) as exc:
        raise RailError("version info has no repository records") from exc
    if not isinstance(repo_records, list) or not isinstance(access_records, list):
        raise RailError("repository metadata is not a list")
    access: dict[int, dict[str, Any]] = {}
    for record in access_records:
        if not isinstance(record, dict):
            raise RailError("invalid repository access record")
        try:
            access[record["repository_id"]] = record
        except (KeyError, TypeError) as exc:
            raise RailError("repository access record has no id") from exc
    repositories_out: list[Repository] = []
    for record in repo_records:
        if not isinstance(record, dict):
            raise RailError("invalid repository record")
        try:
            if record["chunk_encrypt_flag"] != 0:
                raise RailError("encrypted repository transfers are unsupported")
            manifests = record["manifest_files"]
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise RailError("a repository must have exactly one manifest")
            manifest = manifests[0]
            if not isinstance(manifest, dict):
                raise RailError("invalid manifest record")
            repository_id = record["repository_id"]
            access_record = access[repository_id]
            cdn_root = record["cdn_root"]
            file_url = manifest["file_url"]
            manifest_size = manifest["manifest_size"]
            manifest_md5 = manifest["manifest_md5"]
            key_hex = access_record["manifest_encrytion_key"]
            algorithm = access_record["manifest_encrytion_algorithm_id"]
        except KeyError as exc:
            raise RailError(f"repository metadata is missing {exc.args[0]!r}") from exc
        except TypeError as exc:
            raise RailError("invalid repository metadata") from exc
        if not isinstance(cdn_root, str) or not isinstance(file_url, str):
            raise RailError("repository CDN paths must be strings")
        if not isinstance(repository_id, int) or not isinstance(manifest_size, int):
            raise RailError("repository ids and sizes must be integers")
        if not 0 < manifest_size <= _MAX_MANIFEST_INPUT:
            raise RailError("repository manifest size is out of range")
        if not isinstance(manifest_md5, str) or not isinstance(key_hex, str):
            raise RailError("repository checksums must be strings")
        if not isinstance(algorithm, int):
            raise RailError("name encryption algorithm must be an integer")
        repositories_out.append(
            Repository(
                repository_id=repository_id,
                cdn_root=cdn_root,
                manifest_url=cdn_root + file_url,
                manifest_size=manifest_size,
                manifest_md5=manifest_md5,
                key_hex=key_hex,
                algorithm=algorithm,
            )
        )
    return repositories_out, access


def repositories(info: dict) -> list[Repository]:
    """Parse the ``launcher_version_info`` record into repository descriptors."""
    try:
        return _repository_list(info)[0]
    except RailError:
        raise
    except Exception as exc:
        raise RailError("invalid repository metadata") from exc


def _parse_chunk(fields: _Fields) -> Chunk:
    return Chunk(
        size=fields.integer(1),
        compressed_size=fields.integer(2),
        compressed_md5=fields.blob(4),
        md5=fields.blob(5),
        method=fields.integer(7),
    )


def _parse_entry(fields: _Fields, path: str) -> Entry:
    size = fields.integer(1)
    attribute = fields.integer(2)
    flag = fields.integer(3)
    md5 = fields.blob(4)
    chunks = tuple(_parse_chunk(chunk) for chunk in fields.messages(5))
    if flag != 0 or attribute not in (16, 32, 128):
        raise RailError(f"unsupported attributes on {path!r}")
    is_dir = bool(attribute & 16)
    if is_dir:
        if size != 0 or chunks:
            raise RailError(f"directory {path!r} has a size or chunks")
        return Entry(path=path, size=0, md5=md5, is_dir=True, chunks=())
    if sum(chunk.size for chunk in chunks) != size:
        raise RailError(f"chunks of {path!r} do not add up to its size")
    if size == 0 and not md5:
        md5 = hashlib.md5(b"").digest()  # noqa: S324 - the publisher's own checksum
    if len(md5) != 16:
        raise RailError(f"file {path!r} has no valid MD5")
    return Entry(path=path, size=size, md5=md5, is_dir=False, chunks=chunks)


def _parse_manifest(data: bytes, repo: Repository) -> list[Entry]:
    if not isinstance(data, (bytes, bytearray)) or not 16 <= len(data) <= _MAX_MANIFEST_INPUT:
        raise RailError("manifest input has an invalid size")
    raw = qqtea_decrypt(bytes(data), PI_KEY)
    manifest = _Fields(raw)
    header = _Fields(manifest.blob(1))
    body = manifest.blob(2)
    if header.integer(1) != 2:
        raise RailError("unsupported manifest version")
    body_size = header.integer(2)
    if body_size > _MAX_MANIFEST_BODY:
        raise RailError("manifest body is too large")
    if header.integer(3) != len(body):
        raise RailError("manifest body length does not match its header")
    if header.integer(4) != 3:
        raise RailError("unsupported manifest compression")
    uncompressed = _zstd_decompress(body, body_size, "manifest")
    if len(uncompressed) != body_size:
        raise RailError("decompressed manifest has the wrong size")
    # The repository holds a few header fields and one field per file.
    repository = _Fields(uncompressed, limit=_MAX_FILES + 64)
    if repository.integer(1) != repo.repository_id:
        raise RailError("manifest belongs to another repository")
    if repo.algorithm != 6:
        raise RailError("unsupported file-name encryption")
    try:
        key = bytes.fromhex(repo.key_hex)
    except ValueError as exc:
        raise RailError("invalid manifest encryption key") from exc
    if len(key) != 16:
        raise RailError("manifest encryption key has the wrong length")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
        plain = decryptor.update(repository.blob(12)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(plain) + unpadder.finalize()
    except Exception as exc:
        raise RailError("cannot decrypt the manifest file names") from exc
    enc_info = _Fields(plain)
    try:
        paths = [path.decode("utf-8") for path in enc_info.blobs(1)]
    except UnicodeDecodeError as exc:
        raise RailError("manifest path is not UTF-8") from exc
    files = repository.messages(13)
    if len(paths) != len(files):
        raise RailError("manifest path count differs from its file count")
    if len(files) > _MAX_FILES:
        raise RailError("manifest lists too many files")
    pack = _Fields(repository.blob(11))
    if pack.integer(3) != 0:
        raise RailError("encrypted chunks are unsupported")
    return [_parse_entry(fields, path) for fields, path in zip(files, paths, strict=True)]


def parse_manifest(data: bytes, repo: Repository) -> list[Entry]:
    """Parse a verified rail_setup manifest into its directory and file entries."""
    try:
        return _parse_manifest(data, repo)
    except RailError:
        raise
    except Exception as exc:
        raise RailError("invalid repository manifest") from exc


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------
def chunk_url(cdn_root: str, chunk: Chunk) -> str:
    """The content-addressed CDN URL of ``chunk``."""
    digest = chunk.md5.hex()
    return f"{cdn_root}/chunksv2/{digest[:2]}/{digest}{chunk.size:x}.wgc"


def decode_chunk(data: bytes, chunk: Chunk) -> bytes:
    """Verify and decompress one wire chunk, checking both sizes and both MD5s."""
    if not isinstance(data, (bytes, bytearray)):
        raise RailError("chunk data is not bytes")
    data = bytes(data)
    if not 0 <= chunk.size <= _MAX_CHUNK_RAW:
        raise RailError("chunk raw size is out of range")
    if not 0 <= chunk.compressed_size <= _MAX_CHUNK_WIRE:
        raise RailError("chunk wire size is out of range")
    if len(data) != chunk.compressed_size:
        raise RailError("chunk wire size does not match the manifest")
    if hashlib.md5(data).digest() != chunk.compressed_md5:  # noqa: S324
        raise RailError("chunk wire MD5 does not match the manifest")
    method = chunk.method
    if method == 0:
        out = data
    elif method == 1:
        if not data or data[0] > 28:
            raise RailError("unsupported LZMA2 dictionary")
        dictionary = (2 | (data[0] & 1)) << (data[0] // 2 + 11)
        decoder = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA2, "dict_size": dictionary}],
        )
        try:
            out = decoder.decompress(data[1:], max_length=chunk.size + 1)
        except Exception as exc:
            raise RailError("invalid LZMA2 chunk") from exc
        if not decoder.eof or decoder.unused_data:
            raise RailError("truncated or trailing LZMA2 data")
    elif method == 2:
        out = lz4_block_decompress(data, chunk.size)
    elif method == 3:
        out = _zstd_decompress(data, chunk.size, "chunk")
    else:
        raise RailError(f"unsupported chunk compression method {method}")
    if len(out) != chunk.size or hashlib.md5(out).digest() != chunk.md5:  # noqa: S324
        raise RailError("chunk content does not match the manifest")
    return out


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstallResult:
    files: int
    bytes: int
    downloaded: int
    kept: int


def _md5_file(path: Path) -> bytes:
    digest = hashlib.md5()  # noqa: S324 - the publisher's own checksum
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _fetch_verified(
    fetch: Callable[[str, int, str], bytes], url: str, size: int, md5_hex: str
) -> bytes:
    data = fetch(url, size, md5_hex)
    if not isinstance(data, (bytes, bytearray)):
        raise RailError(f"{url} did not return bytes")
    data = bytes(data)
    if len(data) != size:
        raise RailError(f"{url} has the wrong size")
    if hashlib.md5(data).hexdigest() != md5_hex.lower():  # noqa: S324
        raise RailError(f"{url} has the wrong MD5")
    return data


def _install_parts(path: str) -> tuple[str, ...]:
    """Normalise an install path to safe components, rejecting Windows-style hazards."""
    if not isinstance(path, str) or not path:
        raise RailError("empty install path")
    if "\0" in path:
        raise RailError(f"NUL byte in install path {path!r}")
    normalised = path.replace("\\", "/")
    if normalised.startswith("/"):
        raise RailError(f"absolute install path {path!r}")
    if ":" in normalised:
        raise RailError(f"drive or stream in install path {path!r}")
    parts = normalised.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RailError(f"unsafe install path {path!r}")
    if parts[0].casefold() == _PARTIAL_DIR:
        raise RailError(f"install path {path!r} is inside Nadare's own temporary folder")
    return tuple(parts)


def _refuse_symlinks(root: Path, parts: Iterable[str]) -> None:
    current = root
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RailError(f"cannot inspect install path: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise RailError(f"symlink in install path: {current}")


def _ensure_partial_dir(dest: Path) -> Path:
    partial = dest / _PARTIAL_DIR
    if partial.is_symlink():
        raise RailError(f"{_PARTIAL_DIR} must not be a symlink")
    try:
        partial.mkdir(exist_ok=True)
    except FileExistsError as exc:
        raise RailError(f"{_PARTIAL_DIR} is not a directory") from exc
    except OSError as exc:
        raise RailError(f"cannot create {_PARTIAL_DIR}: {exc}") from exc
    _sweep_stale_partials(partial)
    return partial


def _sweep_stale_partials(partial: Path) -> None:
    """Delete our own temporary files (``tmp*``) that a killed run left behind."""
    cutoff = time.time() - _STALE_PARTIAL_SECONDS
    try:
        children = list(partial.iterdir())
    except OSError:
        return
    for child in children:
        try:
            info = child.lstat()
            ours = child.name.startswith("tmp") and stat.S_ISREG(info.st_mode)
            if ours and info.st_mtime < cutoff:
                child.unlink()
        except OSError:
            continue


def _drop_empty_partial_dir(dest: Path) -> None:
    try:
        if dest.is_symlink():
            return
        (dest / _PARTIAL_DIR).rmdir()  # only when empty: another run may still be writing
    except OSError:
        pass


def _keep_existing(path: Path, size: int, md5: bytes) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size != size:
            return False
        return _md5_file(path) == md5
    except OSError as exc:
        raise RailError(f"cannot inspect {path}: {exc}") from exc


def _download_file(
    dest: Path,
    parts: tuple[str, ...],
    entry: Entry,
    cdn_root: str,
    fetch: Callable[[str, int, str], bytes],
    cancel: Cancel | None,
    partial: Path,
) -> None:
    target = dest.joinpath(*parts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RailError(f"cannot create the directory of {target}: {exc}") from exc
    _refuse_symlinks(dest, parts)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=partial, delete=False) as out:
            temporary = Path(out.name)
            try:
                for chunk in entry.chunks:
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    url = chunk_url(cdn_root, chunk)
                    wire = _fetch_verified(
                        fetch, url, chunk.compressed_size, chunk.compressed_md5.hex()
                    )
                    out.write(decode_chunk(wire, chunk))
                out.flush()
                os.fsync(out.fileno())
            except Cancelled:
                raise
            except RailError:
                raise
            except Exception as exc:
                raise RailError(f"cannot download {target}: {exc}") from exc
        if temporary.stat().st_size != entry.size or _md5_file(temporary) != entry.md5:
            raise RailError(f"downloaded file {target} does not match the manifest")
        _refuse_symlinks(dest, parts)
        if target.is_symlink():
            raise RailError(f"refusing to replace the symlink {target}")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _install(
    repos: Iterable[Repository],
    dest: Path,
    fetch: Callable[[str, int, str], bytes],
    emit: Callable[[Event], None] | None,
    cancel: Cancel | None,
) -> InstallResult:
    dest = Path(dest)
    if dest.is_symlink():
        raise RailError("the destination must not be a symlink")
    if dest.exists() and not dest.is_dir():
        raise RailError("the destination is not a directory")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RailError(f"cannot create the destination: {exc}") from exc

    entries: list[tuple[Repository, Entry, tuple[str, ...]]] = []
    seen: set[str] = set()
    folder_spelling: dict[str, str] = {}
    file_keys: set[str] = set()
    for repo in repos:
        if cancel is not None:
            cancel.raise_if_cancelled()
        data = _fetch_verified(fetch, repo.manifest_url, repo.manifest_size, repo.manifest_md5)
        if cancel is not None:
            cancel.raise_if_cancelled()
        for entry in parse_manifest(data, repo):
            parts = _install_parts(entry.path)
            key = "/".join(parts).casefold()
            if key in seen:
                raise RailError(f"duplicate or case-colliding path {entry.path!r}")
            seen.add(key)
            # Every folder on the way, by its case-folded name: one spelling only, and never
            # also a file (Windows sees "Foo" and "foo/" as the same entry).
            folders = [parts[:i] for i in range(1, len(parts))]
            if entry.is_dir:
                folders.append(parts)
            for folder in folders:
                folded = "/".join(folder).casefold()
                spelled = folder_spelling.setdefault(folded, "/".join(folder))
                if spelled != "/".join(folder):
                    raise RailError(f"folder spelled two ways: {spelled!r}, {entry.path!r}")
            if not entry.is_dir:
                file_keys.add(key)
            entries.append((repo, entry, parts))
    clash = file_keys & set(folder_spelling)
    if clash:
        raise RailError(f"a file and a folder share the name {sorted(clash)[0]!r}")
    for _repo, _entry, parts in entries:
        _refuse_symlinks(dest, parts)

    for _repo, entry, parts in entries:
        if not entry.is_dir:
            continue
        target = dest.joinpath(*parts)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RailError(f"cannot create directory {target}: {exc}") from exc

    files = [item for item in entries if not item[1].is_dir]
    total_files = len(files)
    total_bytes = sum(entry.size for _repo, entry, _parts in files)
    done_files = 0
    done_bytes = 0
    downloaded = 0
    kept = 0
    partial: Path | None = None
    for repo, entry, parts in files:
        if cancel is not None:
            cancel.raise_if_cancelled()
        target = dest.joinpath(*parts)
        if _keep_existing(target, entry.size, entry.md5):
            kept += 1
        else:
            try:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise RailError(f"refusing to replace the non-file {target}")
            except OSError as exc:
                raise RailError(f"cannot inspect {target}: {exc}") from exc
            if partial is None:
                partial = _ensure_partial_dir(dest)
            _download_file(dest, parts, entry, repo.cdn_root, fetch, cancel, partial)
            downloaded += 1
        done_files += 1
        done_bytes += entry.size
        if emit is not None:
            fraction = done_bytes / total_bytes if total_bytes else 1.0
            emit(
                Event(
                    "progress",
                    f"{done_files}/{total_files} files",
                    progress=fraction,
                )
            )
    if total_files == 0 and emit is not None:
        emit(Event("progress", "0/0 files", progress=1.0))

    for _repo, entry, parts in files:
        target = dest.joinpath(*parts)
        try:
            if target.is_symlink() or not target.is_file():
                raise RailError(f"installed file {target} is missing or is a symlink")
            if target.stat().st_size != entry.size or _md5_file(target) != entry.md5:
                raise RailError(f"installed file {target} does not match the manifest")
        except RailError:
            raise
        except OSError as exc:
            raise RailError(f"cannot verify installed file {target}: {exc}") from exc

    if partial is not None:
        try:
            partial.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return InstallResult(
        files=total_files,
        bytes=total_bytes,
        downloaded=downloaded,
        kept=kept,
    )


def install(
    repos: Iterable[Repository],
    dest: Path,
    *,
    fetch: Callable[[str, int, str], bytes],
    emit: Callable[[Event], None] | None = None,
    cancel: Cancel | None = None,
) -> InstallResult:
    """Fetch and install every file of ``repos`` below ``dest``, verifying all bytes."""
    try:
        return _install(repos, dest, fetch, emit, cancel)
    except (RailError, Cancelled):
        _drop_empty_partial_dir(dest)
        raise
    except Exception as exc:
        _drop_empty_partial_dir(dest)
        raise RailError(f"install failed: {exc}") from exc
