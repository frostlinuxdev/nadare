# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Yostar direct downloads from a CI-signed manifest.

Yostar's launcher API is authenticated with a per-game *salt* that must never
ship in a GPL client.  A scheduled CI job
(``ci/yostar-manifests/``) signs the calls with the salt and publishes a plain,
normalised manifest.  This module is the client half:

* :func:`load_manifest` — read/validate a manifest (https URL, local path, or an
  index that points at per-game manifests);
* :func:`plan` — which files are missing or differ (size first, then CRC-64/XZ);
* :func:`sync` — download changed files through the resumable
  :mod:`nadare.core.download` machinery and verify their CRC-64/XZ.

Yostar ships no binary diffs, so changed files are re-downloaded whole.  The
download itself already lands in ``<dest>.part`` and is renamed atomically after
verification.  The crash-safe update journal in :mod:`nadare.core.journal` is
*not* used here: it is designed for patch transactions that overwrite existing
files while keeping rollback copies, whereas a whole-file, resumable,
idempotent sync can simply re-fetch anything a crash left behind.  Wrapping it
would double disk I/O (backup + copy per file) without adding safety.

No salt is ever read, stored or logged by this module; :func:`signature` and
:func:`authorization_header` are shared with the CI generator only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diskspace, download, hosting, paths
from .events import Cancel, Cancelled, Emitter, Event, emitter

log = logging.getLogger(__name__)

#: DownloadError text that describes a refused URL/host rather than a network fault.
_SECURITY_DOWNLOAD_MARKERS = (
    "refusing",
    "disallowed",
    "untrusted",
    "redirect",
    "invalid url",
    "unsupported url",
    "no host",
)

__all__ = [
    "GAME_TAGS",
    "ALLOWED_HOST_SUFFIX",
    "ManifestError",
    "Manifest",
    "ManifestFile",
    "crc64",
    "crc64_file",
    "Crc64",
    "normalize_crc64",
    "build_head",
    "head_string",
    "signature",
    "authorization_header",
    "signed_headers",
    "safe_relative_path",
    "host_allowed",
    "base_url_trusted",
    "file_url",
    "configured_manifest",
    "load_manifest",
    "MediaError",
    "Banner",
    "Media",
    "configured_media",
    "load_media",
    "media_url_allowed",
    "cache_media",
    "plan",
    "sync",
]

#: Known game tags.  Values are the Nadare backend kinds that consume them.
GAME_TAGS: dict[str, str] = {
    "StellaSora_EN": "stellasora-yostar",
    "Arknights_EN": "arknights-yostar",
}

#: Hosts under this suffix are always trusted Yostar CDNs.
ALLOWED_HOST_SUFFIX = ".yo-star.com"

#: Loopback hosts are trusted for local test servers.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: The request Accept header the launcher API expects on signed calls.
ACCEPT = "application/json, text/plain, */*"

#: Schema version of the normalised manifest.
SCHEMA = 1


class ManifestError(RuntimeError):
    """A manifest is missing, malformed, or failed verification."""


# ---------------------------------------------------------------------------
# CRC-64/XZ (reflected ECMA-182).  liblzma exposes this as ``lzma_crc64`` and
# runs orders of magnitude faster than a pure-Python byte loop, so prefer it
# when present and fall back to the table below otherwise.  The native call
# takes the running CRC directly as its third argument, matching the
# incremental contract of :func:`crc64` (``crc64(b, crc64(a)) == crc64(a+b)``).
# ---------------------------------------------------------------------------
_CRC64_POLY = 0xC96C5795D7870F42
_CRC64_MASK = 0xFFFFFFFFFFFFFFFF
_CRC64_TABLE: list[int] = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ _CRC64_POLY if (_crc & 1) else _crc >> 1
    _CRC64_TABLE.append(_crc)
del _i, _crc


def _load_native_crc64() -> Any:
    """Return liblzma's ``lzma_crc64`` as a ctypes function, or ``None``."""
    names: list[str] = []
    try:
        found = ctypes.util.find_library("lzma")
    except OSError:
        found = None
    if found:
        names.append(found)
    names += ["liblzma.so.5", "liblzma.so"]
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            fn = lib.lzma_crc64
        except (OSError, AttributeError):
            continue
        fn.restype = ctypes.c_uint64
        fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint64]
        return fn
    return None


_NATIVE_CRC64 = _load_native_crc64()


def _crc64_python(data: bytes, crc: int = 0) -> int:
    value = (crc ^ _CRC64_MASK) & _CRC64_MASK
    table = _CRC64_TABLE
    for byte in data:
        value = (value >> 8) ^ table[(value ^ byte) & 0xFF]
    return (value ^ _CRC64_MASK) & _CRC64_MASK


def _crc64_native(data: bytes, crc: int = 0) -> int:
    if _NATIVE_CRC64 is None:
        raise RuntimeError("liblzma crc64 is unavailable")
    if not isinstance(data, bytes):
        data = bytes(data)
    return int(_NATIVE_CRC64(data, len(data), crc & _CRC64_MASK))


def crc64(data: bytes, crc: int = 0) -> int:
    """Return the CRC-64/XZ of ``data``, continuing from ``crc``."""
    if _NATIVE_CRC64 is not None:
        return _crc64_native(data, crc)
    return _crc64_python(data, crc)


class Crc64:
    """Incremental CRC-64/XZ helper: ``update(block)`` then read ``value``."""

    __slots__ = ("_value",)

    def __init__(self, crc: int = 0) -> None:
        self._value = crc & _CRC64_MASK

    def update(self, data: bytes) -> Crc64:
        self._value = crc64(data, self._value)
        return self

    def reset(self) -> None:
        self._value = 0

    @property
    def value(self) -> int:
        return self._value


def crc64_file(path: str | os.PathLike[str], *, chunk: int = 1 << 20) -> int:
    """Stream ``path`` and return its CRC-64/XZ (native when available)."""
    hasher = Crc64()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.value


def normalize_crc64(value: int | str | None) -> int:
    """Normalise a decimal/hex CRC-64 (with or without ``0x``) to an int."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("invalid CRC-64")
    if isinstance(value, int):
        return value & _CRC64_MASK
    text = str(value).strip()
    if not text:
        return 0
    if text.lower().startswith("0x"):
        return int(text, 16) & _CRC64_MASK
    digits = text.lstrip("+-")
    if digits.isdigit():
        return int(text, 10) & _CRC64_MASK
    if re.fullmatch(r"[0-9a-fA-F]+", text):
        return int(text, 16) & _CRC64_MASK
    raise ValueError(f"invalid CRC-64: {value!r}")


# ---------------------------------------------------------------------------
# signing (shared with the CI generator; contains no salt)
# ---------------------------------------------------------------------------
def build_head(game_tag: str, version: str, time: int | None = None) -> dict[str, Any]:
    """Build the ordered ``head`` object that the launcher API signs."""
    return {
        "game_tag": str(game_tag),
        "time": int(time if time is not None else time_seconds()),
        "version": str(version),
    }


def time_seconds() -> int:
    return int(time.time())


def head_string(head: dict[str, Any]) -> str:
    """Serialise ``head`` exactly as the signer does (compact, ordered)."""
    return json.dumps(head, separators=(",", ":"))


def signature(head_str: str, data_str: str, salt: str) -> str:
    """``md5(head_str + data_str + salt)`` as a lowercase hex digest."""
    return hashlib.md5((head_str + data_str + salt).encode("utf-8")).hexdigest()  # noqa: S324


def authorization_header(
    game_tag: str,
    version: str,
    salt: str,
    *,
    data_str: str = "",
    time: int | None = None,
) -> str:
    """Return the ``Authorization`` header value (never log this)."""
    head = build_head(game_tag, version, time)
    sign = signature(head_string(head), data_str, salt)
    return json.dumps({"head": head, "sign": sign}, separators=(",", ":"))


def signed_headers(
    game_tag: str,
    version: str,
    salt: str,
    *,
    data_str: str = "",
    time: int | None = None,
) -> dict[str, str]:
    """Return the full signed request headers for a launcher API call."""
    return {
        "Authorization": authorization_header(
            game_tag, version, salt, data_str=data_str, time=time
        ),
        "Accept": ACCEPT,
    }


# ---------------------------------------------------------------------------
# manifest model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ManifestFile:
    path: str
    size: int
    crc64: int


@dataclass(frozen=True)
class Manifest:
    game_tag: str
    version: str
    base_url: str
    generated_at: str
    files: tuple[ManifestFile, ...]
    source: str = ""
    schema: int = SCHEMA


class MediaError(RuntimeError):
    """A media document is missing, malformed, or fails its host check."""


@dataclass(frozen=True)
class Banner:
    """One launcher operation banner (image URL + optional click-through URL)."""

    image: str
    url: str = ""


@dataclass(frozen=True)
class Media:
    """Official launcher art: a hero background plus operation banners."""

    game_tag: str
    generated_at: str
    background: str | None
    banners: tuple[Banner, ...] = ()
    source: str = ""


def safe_relative_path(path: Any) -> str:
    """Validate a manifest path, returning its POSIX-relative normal form.

    Absolute paths, drive-letter paths, ``..`` and anything that escapes the
    install root are rejected.
    """
    try:
        return paths.check_relative(path, paths.MANIFEST_PATHS)
    except paths.PathProblem as exc:
        if exc.code == "not-string":
            message = f"manifest path is not a string: {path!r}"
        elif exc.code == "empty":
            message = "empty manifest path"
        elif exc.code == "nul":
            message = f"NUL byte in manifest path: {path!r}"
        elif exc.code == "absolute":
            message = f"absolute manifest path: {path!r}"
        elif exc.code == "escape":
            message = f"path escapes the install dir: {path!r}"
        else:
            message = f"unsafe manifest path: {path!r}"
        raise ManifestError(message) from None


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise ManifestError(f"invalid {what}: {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"invalid {what}: {value!r}") from exc
    if number < 0:
        raise ManifestError(f"negative {what}: {value!r}")
    return number


def _parse_manifest(payload: Any, source: str = "") -> Manifest:
    if not isinstance(payload, dict):
        raise ManifestError("manifest root is not an object")
    schema = payload.get("schema", 1)
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or not 1 <= schema <= SCHEMA
    ):
        raise ManifestError(
            f"unsupported manifest schema {schema!r} (this Nadare reads up to {SCHEMA})"
        )
    game_tag = payload.get("game_tag")
    version = payload.get("version")
    base_url = payload.get("base_url")
    generated_at = payload.get("generated_at", "")
    files_raw = payload.get("files")
    if not isinstance(game_tag, str) or not game_tag:
        raise ManifestError("manifest has no game_tag")
    if not isinstance(version, str) or not version:
        raise ManifestError("manifest has no version string")
    if not isinstance(base_url, str) or not base_url:
        raise ManifestError("manifest has no base_url")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ManifestError(f"manifest base_url is not an http(s) URL: {base_url!r}")
    if not isinstance(files_raw, list):
        raise ManifestError("manifest files is not a list")
    files: list[ManifestFile] = []
    seen: set[str] = set()
    for item in files_raw:
        if not isinstance(item, dict):
            raise ManifestError(f"manifest file entry is not an object: {item!r}")
        rel = safe_relative_path(item.get("path"))
        # Two entries for one file would race on a single ``.part``; compare
        # case-insensitively, as Windows (and so Wine) resolves names.
        folded = str(rel).lower()
        if folded in seen:
            raise ManifestError(f"manifest lists a path twice (duplicate): {str(rel)!r}")
        seen.add(folded)
        size = _as_int(item.get("size"), "size")
        try:
            crc = normalize_crc64(item.get("crc64", item.get("hash")))
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        files.append(ManifestFile(rel, size, crc))
    return Manifest(
        game_tag=game_tag,
        version=version,
        base_url=base_url,
        generated_at=str(generated_at),
        files=tuple(files),
        source=source,
        schema=schema,
    )


def _is_url(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme in ("http", "https")


def _read_bytes(source: str, timeout: int) -> bytes:
    if _is_url(source):
        return download.http_get(
            source, timeout=timeout, redirect_allowed=_redirect_allowed(source)
        )
    try:
        return Path(source).expanduser().read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {source}: {exc}") from exc


def _parse_json(raw: bytes, source: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest {source} is not valid JSON: {exc}") from exc


def _read_json(source: str, timeout: int) -> Any:
    return _parse_json(_read_bytes(source, timeout), source)


def _looks_like_index(payload: Any) -> bool:
    if not isinstance(payload, dict) or "files" in payload:
        return False
    return any(isinstance(v, dict) and "url" in v for v in payload.values())


def _resolve_relative(base_source: str, target: str) -> str:
    if _is_url(target):
        return target
    if _is_url(base_source):
        return urllib.parse.urljoin(base_source, target)
    return str((Path(base_source).expanduser().resolve().parent / target).resolve())


def _source_is_local(source: str) -> bool:
    """True when a manifest source is a local path or a loopback URL.

    Gates loopback ``base_url`` downloads: a manifest fetched from a remote host
    must not be able to redirect the download at an arbitrary localhost service.
    """
    if not source:
        return True
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in ("http", "https"):
        return True  # a local filesystem path
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _redirect_allowed(
    source: str, base_host: str | None = None, allowed_hosts: set[str] | None = None
) -> Callable[[str], bool]:
    """Build a per-hop redirect guard for a download from ``source``.

    A redirect target must stay on a host allowed by :func:`host_allowed`
    (the ``yo-star`` CDN suffix, an explicit allow-list, or loopback).  A
    loopback target is refused unless the manifest itself came from a local
    source, so a remote manifest cannot bounce a download at localhost (SSRF).
    """

    def _check(target: str) -> bool:
        host = (urllib.parse.urlparse(target).hostname or "").lower()
        if host in _LOOPBACK_HOSTS and not _source_is_local(source):
            return False
        return host_allowed(host, base_host, allowed_hosts)

    return _check


def load_manifest(source: str, *, game_tag: str | None = None, timeout: int = 60, _depth: int = 0
) -> Manifest:
    """Load and validate a manifest from a URL, a local path, or an index.

    When ``source`` is an index (a mapping of tag to ``{"url": ...}``), pass
    ``game_tag`` to select the per-game manifest it points at.
    """
    if not source or not str(source).strip():
        raise ManifestError("no manifest source configured")
    source = str(source).strip()
    payload = _read_json(source, timeout)
    while _looks_like_index(payload):
        if not game_tag:
            raise ManifestError("manifest index needs a game_tag to select an entry")
        entry = payload.get(game_tag)
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ManifestError(f"manifest index has no entry for {game_tag!r}")
        if _depth >= 4:
            raise ManifestError("manifest index nesting too deep")
        target = _resolve_relative(source, str(entry["url"]))
        raw = _read_bytes(target, timeout)
        expected = entry.get("sha256")
        if isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            if hashlib.sha256(raw).hexdigest() != expected.lower():
                raise ManifestError(
                    f"manifest {target} does not match the sha256 in the index"
                )
        payload = _parse_json(raw, target)
        source = target
        _depth += 1
    manifest = _parse_manifest(payload, source)
    if game_tag and manifest.game_tag != game_tag:
        raise ManifestError(f"manifest is for {manifest.game_tag!r}, expected {game_tag!r}")
    return manifest


def configured_manifest(
    profile: dict[str, Any] | None = None, env: dict[str, str] | None = None
) -> str:
    """Resolve the configured manifest source (env, ``manifest_index``, file)."""
    environment = os.environ if env is None else env
    raw = str(environment.get("YOSTAR_MANIFEST_INDEX", "") or "").strip()
    if raw:
        return raw
    profile = profile or {}
    for key in ("manifest_index", "manifest_file"):
        value = profile.get(key)
        if value in (None, ""):
            extra = profile.get("extra")
            if isinstance(extra, dict):
                value = extra.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return hosting.yostar_index_url()


# ---------------------------------------------------------------------------
# official media (launcher art)
# ---------------------------------------------------------------------------
def _parse_media(payload: Any, source: str = "") -> Media:
    if not isinstance(payload, dict):
        raise MediaError("media root is not an object")
    game_tag = payload.get("game_tag")
    if not isinstance(game_tag, str) or not game_tag:
        raise MediaError("media has no game_tag")
    background = payload.get("background")
    if background is not None and not isinstance(background, str):
        raise MediaError("media background is not a string")
    banners_raw = payload.get("banners", [])
    if not isinstance(banners_raw, list):
        raise MediaError("media banners is not a list")
    banners: list[Banner] = []
    for item in banners_raw:
        if not isinstance(item, dict):
            raise MediaError(f"malformed banner: {item!r}")
        image = item.get("image")
        if not image:
            continue
        banners.append(Banner(str(image), str(item.get("url") or "")))
    return Media(
        game_tag=game_tag,
        generated_at=str(payload.get("generated_at") or ""),
        background=str(background) if background else None,
        banners=tuple(banners),
        source=source,
    )


def load_media(
    source: str, game_tag: str | None = None, *, timeout: int = 60, _depth: int = 0
) -> Media:
    """Load a media document from a URL, a local path, or a manifest index.

    Mirrors :func:`load_manifest`: when ``source`` is an index that carries a
    ``media`` entry for ``game_tag``, that per-game media document is loaded.
    """
    if not source or not str(source).strip():
        raise MediaError("no media source configured")
    source = str(source).strip()
    payload = _read_json(source, timeout)
    if _looks_like_index(payload):
        if not game_tag:
            raise MediaError("media index needs a game_tag to select an entry")
        entry = payload.get(game_tag)
        if not isinstance(entry, dict):
            raise MediaError(f"media index has no entry for {game_tag!r}")
        target = entry.get("media")
        if not target:
            raise MediaError(f"media index has no media for {game_tag!r}")
        if _depth >= 4:
            raise MediaError("media index nesting too deep")
        resolved = _resolve_relative(source, str(target))
        return load_media(resolved, game_tag=game_tag, timeout=timeout, _depth=_depth + 1)
    media = _parse_media(payload, source)
    if game_tag and media.game_tag != game_tag:
        raise MediaError(f"media is for {media.game_tag!r}, expected {game_tag!r}")
    return media


def configured_media(
    profile: dict[str, Any] | None = None, env: dict[str, str] | None = None
) -> str:
    """Resolve a media source (explicit file, then the manifest index).

    Order: ``YOSTAR_MEDIA_FILE``, profile ``media_file``, ``YOSTAR_MANIFEST_INDEX``,
    profile ``manifest_index``, then Nadare's published default index.  A single
    manifest document has no media, so ``manifest_file`` is never used here.
    """
    environment = os.environ if env is None else env
    raw = str(environment.get("YOSTAR_MEDIA_FILE", "") or "").strip()
    if raw:
        return raw
    profile = profile or {}

    def _profile_value(key: str):
        value = profile.get(key)
        if value in (None, ""):
            extra = profile.get("extra")
            if isinstance(extra, dict):
                value = extra.get(key)
        return value

    value = _profile_value("media_file")
    if value not in (None, ""):
        return str(value).strip()
    raw = str(environment.get("YOSTAR_MANIFEST_INDEX", "") or "").strip()
    if raw:
        return raw
    value = _profile_value("manifest_index")
    if value not in (None, ""):
        return str(value).strip()
    return hosting.yostar_index_url()


def media_url_allowed(url: str) -> bool:
    """True for an https URL on a ``*.yo-star.com`` host (no port/userinfo)."""
    if not isinstance(url, str) or not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or parsed.port is not None:
        return False
    host = (parsed.hostname or "").lower()
    from .artwork import _is_local_host

    if host in _LOOPBACK_HOSTS or host == "localhost" or _is_local_host(host):
        return False
    return host_allowed(host, None)


def _media_host(url: str) -> tuple[str, ...]:
    """The single publisher host of ``url`` as an artwork allow-list.

    ``artwork.download`` grants official provenance only when its allow-list is
    non-empty, so a validated ``*.yo-star.com`` media URL must pass its own
    host rather than an empty tuple (which would cache it as third-party).
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (host,) if host else ()


def cache_media(
    media: Media, slug: str, *, deadline: float | None = None, cancel=None
) -> dict[str, Any]:
    """Download a media document's background/banners into the art cache.

    The hero lands at ``{slug}-official-hero.{ext}`` and the banners at
    ``{slug}-official-banner-N.{ext}`` (N in document order).  Official files
    carry a source-URL sidecar so a changed launcher URL forces a re-download
    and the old third-party ``{slug}-hero`` cache can never shadow them.  Only
    ``*.yo-star.com`` https URLs are fetched; every failure is swallowed.
    ``deadline`` (an absolute ``time.monotonic()`` value) and ``cancel`` stop a
    slow launcher host from keeping the startup page waiting.
    """
    from . import artwork

    result: dict[str, Any] = {"hero": None, "banners": []}

    def stopped() -> bool:
        return bool(cancel is not None and cancel.cancelled) or (
            deadline is not None and time.monotonic() >= deadline
        )

    def fetch(url: str, kind: str) -> Path | None:
        if stopped():
            return None
        timeout = artwork.DOWNLOAD_TIMEOUT
        if deadline is not None:
            timeout = max(1, min(timeout, int(deadline - time.monotonic())))
        return artwork.download_compat(
            url, slug, kind, _media_host(url), timeout=timeout, cancel=cancel
        )

    if media.background and media_url_allowed(media.background):
        path = fetch(media.background, "official-hero")
        if path is not None:
            result["hero"] = str(path)
    for index, banner in enumerate(media.banners):
        if stopped():
            break
        if not media_url_allowed(banner.image):
            continue
        path = fetch(banner.image, f"official-banner-{index}")
        if path is not None:
            result["banners"].append({"image_path": str(path), "url": banner.url})
    return result


# ---------------------------------------------------------------------------
# planning / syncing
# ---------------------------------------------------------------------------
def _clamp_workers(workers: int) -> int:
    try:
        number = int(workers)
    except (TypeError, ValueError):
        number = 6
    return max(1, min(16, number))


def plan(
    manifest: Manifest,
    install_dir: str | os.PathLike[str],
    *,
    workers: int = 6,
    cancel: Cancel | None = None,
    verify: bool = True,
) -> list[ManifestFile]:
    """Return the manifest files that are missing or differ on disk.

    ``verify=False`` stops at the size check (right after a sync that hashed every file).

    Sizes are filtered first (cheap), then CRCs are computed in a pool so
    updating an existing install does not serialise on disk reads.
    """
    root = Path(install_dir).expanduser()
    pending: list[tuple[int, ManifestFile, Path]] = []
    results: list[ManifestFile | None] = [None] * len(manifest.files)
    for index, entry in enumerate(manifest.files):
        target = root / entry.path
        try:
            stat = target.stat()
        except OSError:
            results[index] = entry
            continue
        if not target.is_file() or stat.st_size != entry.size:
            results[index] = entry
            continue
        if verify:
            pending.append((index, entry, target))

    def needs_update(
        item: tuple[int, ManifestFile, Path],
    ) -> tuple[int, ManifestFile | None]:
        if cancel is not None:
            cancel.raise_if_cancelled()
        index, entry, target = item
        try:
            actual = crc64_file(target)
        except OSError:
            return index, entry
        return index, (None if actual == entry.crc64 else entry)

    count = _clamp_workers(workers)
    if pending and count > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=count) as executor:
            checked = list(executor.map(needs_update, pending))
    else:
        checked = [needs_update(item) for item in pending]
    for index, entry in checked:
        results[index] = entry
    return [entry for entry in results if entry is not None]


def file_url(manifest: Manifest, entry: ManifestFile) -> str:
    """Build the CDN URL for one manifest file under ``base_url``."""
    quoted = urllib.parse.quote(entry.path, safe="/")
    return manifest.base_url.rstrip("/") + "/" + quoted


def host_allowed(
    host: str | None, base_host: str | None, allowed_hosts: set[str] | None = None
) -> bool:
    """Whether ``host`` may be fetched from.

    ``yo-star.com`` and ``*.yo-star.com`` are always allowed, as are loopback
    hosts used by local test servers.  Otherwise ``host`` is allowed only when
    it appears in an explicit ``allowed_hosts`` allowlist.  ``base_host`` is
    kept for call-site compatibility but is *not* trusted: a manifest may not
    vouch for its own host.
    """
    host = (host or "").lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    if host == ALLOWED_HOST_SUFFIX.lstrip(".") or host.endswith(ALLOWED_HOST_SUFFIX):
        return True
    if allowed_hosts is not None:
        return host in {str(h).lower() for h in allowed_hosts}
    return False


def base_url_trusted(url: str, allowed_hosts: set[str] | None = None) -> bool:
    """Whether game files may be downloaded from ``url``.

    Rejects embedded credentials and any non-https scheme, except for loopback
    hosts where plain http is allowed for local test servers.  The hostname
    must then satisfy :func:`host_allowed`.
    """
    parsed = urllib.parse.urlparse(url or "")
    if parsed.username or parsed.password:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    if hostname in _LOOPBACK_HOSTS:
        return parsed.scheme in ("http", "https")
    if parsed.scheme != "https":
        return False
    return host_allowed(hostname, hostname, allowed_hosts)


def _format_size(size: float) -> str:
    return diskspace.human_size(size)


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "ETA --"
    total = max(0, int(seconds))
    if total >= 3600:
        return f"ETA {total // 3600}h{(total % 3600) // 60:02d}m"
    return f"ETA {total // 60}m{total % 60:02d}s"


def _format_progress(done: int, total: int, speed: float, eta: float | None) -> str:
    speed_text = diskspace.human_speed(speed)
    return (
        f"sync: {_format_size(done)}/{_format_size(total)} · "
        f"{speed_text} · {_format_eta(eta)}"
    )


class _SyncProgress:
    """Thread-safe byte aggregator that rate-limits progress events.

    Each worker reports the absolute number of bytes done for its file via
    :meth:`set`; the aggregator sums those, keeps a 5 s moving average of the
    speed and emits at most one event per ``interval`` seconds.
    """

    def __init__(
        self,
        emit: Emitter,
        total: int,
        *,
        interval: float = 0.25,
        window: float = 5.0,
    ) -> None:
        self._emit = emit
        self._total = total
        self._interval = interval
        self._window = window
        self._lock = threading.Lock()
        self._files: dict[str, int] = {}
        self._done = 0
        self._last_emit = 0.0
        self._samples: deque[tuple[float, int]] = deque()

    def set(self, key: str, done: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._done += done - self._files.get(key, 0)
            self._files[key] = done
            if now - self._last_emit < self._interval:
                return
            self._last_emit = now
            event = self._event_locked(now)
        self._emit(event)

    def flush(self) -> None:
        with self._lock:
            event = self._event_locked(time.monotonic())
        self._emit(event)

    def _event_locked(self, now: float) -> Event:
        speed = self._speed_locked(now)
        done = self._done
        progress = (done / self._total) if self._total else 1.0
        remaining = self._total - done
        eta = (remaining / speed) if speed > 0 and remaining > 0 else None
        return Event(
            "progress",
            _format_progress(done, self._total, speed, eta),
            progress=progress,
            data={
                "stage": "sync",
                "done": done,
                "total": self._total,
                "speed_bps": speed,
                "eta_s": eta,
            },
        )

    def _speed_locked(self, now: float) -> float:
        self._samples.append((now, self._done))
        while len(self._samples) > 1 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0
        started, baseline = self._samples[0]
        elapsed = now - started
        if elapsed <= 0:
            return 0.0
        return max(0.0, (self._done - baseline) / elapsed)


def _refuse_symlinked_parent(root: Path, destination: Path, label: str) -> None:
    """Refuse to write through a symlinked directory below ``root``."""
    try:
        relative = destination.parent.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"path escapes the install dir: {label}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ManifestError(f"refusing to write through a symlinked directory: {label}")
    try:
        inside = destination.parent.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        raise ManifestError(f"path escapes the install dir: {label}")


def _is_network_download_error(exc: Exception) -> bool:
    """True for a failed transfer, but not for a refused host/URL or bad bytes."""
    if isinstance(exc, download.IntegrityError):
        return False
    if not isinstance(exc, download.DownloadError):
        return False
    text = str(exc).lower()
    return not any(marker in text for marker in _SECURITY_DOWNLOAD_MARKERS)


def _sync_entry(
    entry: ManifestFile,
    *,
    root: Path,
    manifest: Manifest,
    pool: download.HttpClientPool,
    progress: _SyncProgress,
    cancel: Cancel | None,
    attempts: int,
    allowed_hosts: set[str] | None,
    base_host: str | None,
    chunk: int,
) -> None:
    """Download and verify one manifest file (runs inside a worker thread)."""
    url = file_url(manifest, entry)
    host = urllib.parse.urlparse(url).hostname
    if not host_allowed(host, base_host, allowed_hosts):
        raise ManifestError(f"refusing to download {url}: host {host!r} is not allowed")
    destination = root / entry.path
    _refuse_symlinked_parent(root, destination, entry.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    key = entry.path
    last_error = "checksum mismatch"

    for attempt in range(attempts):
        if cancel is not None:
            cancel.raise_if_cancelled()
        hasher = Crc64()
        if part.exists():
            progress.set(key, part.stat().st_size)

        def on_progress(_count: int, done: int, _total: int, _key: str = key) -> None:
            progress.set(_key, done)

        try:
            download.stream_download(
                url,
                destination,
                pool=pool,
                hasher=hasher,
                expected_size=entry.size,
                redirect_allowed=_redirect_allowed(
                    manifest.source, base_host, allowed_hosts
                ),
                cancel=cancel,
                chunk=chunk,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - retry transient failures
            if isinstance(exc, Cancelled):
                raise
            last_error = str(exc)
            if attempt == attempts - 1:
                part.unlink(missing_ok=True)
                if _is_network_download_error(exc):
                    log.info("download failed for %s: %s", url, exc)
                    raise ManifestError(
                        "Could not download the game files. Check your internet "
                        "connection and try again."
                    ) from exc
                raise ManifestError(f"download failed for {entry.path}: {exc}") from exc
            continue

        if hasher.value != entry.crc64:
            last_error = f"crc64 mismatch for {entry.path}"
            part.unlink(missing_ok=True)
            progress.set(key, 0)
            if attempt == attempts - 1:
                raise ManifestError(last_error)
            continue

        try:
            os.replace(part, destination)
        except OSError as exc:
            raise ManifestError(f"could not finalise {entry.path}: {exc}") from exc
        progress.set(key, entry.size)
        return

    raise ManifestError(last_error)


@contextmanager
def _install_lock(install_dir: Path) -> Iterator[None]:
    """Refuse to run two syncs into one install tree (they share ``.part`` files)."""
    key = hashlib.sha256(str(install_dir.resolve()).encode()).hexdigest()[:16]
    lock_path = paths.state_dir() / "locks" / f"sync-{key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ManifestError(
                f"another install or update is already running for {install_dir}"
            ) from None
        yield
    finally:
        os.close(fd)


def sync(
    manifest: Manifest,
    install_dir: str | os.PathLike[str],
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    *,
    retries: int = 2,
    allowed_hosts: set[str] | None = None,
    workers: int = 6,
    chunk: int = download.DEFAULT_CHUNK,
) -> dict[str, Any]:
    """Serialise :func:`_sync` per install directory."""
    with _install_lock(Path(install_dir).expanduser()):
        return _sync(
            manifest,
            install_dir,
            emit,
            cancel,
            retries=retries,
            allowed_hosts=allowed_hosts,
            workers=workers,
            chunk=chunk,
        )


def _sync(
    manifest: Manifest,
    install_dir: str | os.PathLike[str],
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    *,
    retries: int = 2,
    allowed_hosts: set[str] | None = None,
    workers: int = 6,
    chunk: int = download.DEFAULT_CHUNK,
) -> dict[str, Any]:
    """Download missing/changed files from ``manifest`` into ``install_dir``.

    Up to ``workers`` (1-16) files are fetched in parallel, each over a
    keep-alive HTTP connection, and every byte is CRC-64/XZ-hashed as it is
    written so verification needs no second read.  Returns a summary dict.

    Large single files are *not* split into parallel Range parts: doing so
    would multiply CDN connections and complicate cancellation/resume for a
    case (a handful of very large files) that is not the bottleneck here.
    """
    say = emitter(emit)
    root = Path(install_dir).expanduser()
    parsed_base = urllib.parse.urlparse(manifest.base_url)
    base_host = parsed_base.hostname
    if not base_url_trusted(manifest.base_url, allowed_hosts):
        if parsed_base.scheme != "https" and (base_host or "").lower() not in _LOOPBACK_HOSTS:
            raise ManifestError(
                f"refusing plain http download (https required): {manifest.base_url}"
            )
        raise ManifestError(
            f"refusing to download from untrusted host {base_host!r} "
            f"(manifest base_url {manifest.base_url!r})"
        )
    # FRESH-5: loopback is only a test affordance; a manifest fetched from a
    # remote host may not point the download at a localhost service (SSRF).
    if (base_host or "").lower() in _LOOPBACK_HOSTS and not _source_is_local(manifest.source):
        raise ManifestError(
            f"refusing a loopback download base_url from a remote manifest: "
            f"{manifest.base_url!r}"
        )
    worker_count = _clamp_workers(workers)
    todo = plan(manifest, root, workers=worker_count, cancel=cancel)

    total = sum(entry.size for entry in todo)
    if todo:
        # Reserve only the bytes not already sitting in resumable .part files.
        pending_bytes = total
        for entry in todo:
            part = root / entry.path
            part = part.with_name(part.name + ".part")
            try:
                pending_bytes -= part.stat().st_size
            except OSError:
                pass
        diskspace.check_space(root, max(0, pending_bytes))
    if not todo:
        say(
            Event(
                "progress",
                "sync: nothing to do",
                progress=1.0,
                data={"stage": "sync", "done": 0, "total": 0},
            )
        )
        return {
            "game_tag": manifest.game_tag,
            "version": manifest.version,
            "downloaded": 0,
            "skipped": len(manifest.files),
            "bytes": 0,
            "files": [],
        }

    attempts = max(1, int(retries))
    progress = _SyncProgress(say, total)
    pool = download.HttpClientPool()
    stop = threading.Event()

    def work(entry: ManifestFile) -> None:
        if stop.is_set():
            return
        _sync_entry(
            entry,
            root=root,
            manifest=manifest,
            pool=pool,
            progress=progress,
            cancel=cancel,
            attempts=attempts,
            allowed_hosts=allowed_hosts,
            base_host=base_host,
            chunk=chunk,
        )

    try:
        if worker_count == 1 or len(todo) == 1:
            for entry in todo:
                work(entry)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(work, entry) for entry in todo]
                for future in futures:
                    try:
                        future.result()
                    except BaseException:
                        stop.set()
                        for other in futures:
                            other.cancel()
                        raise
    finally:
        pool.close()

    progress.flush()
    return {
        "game_tag": manifest.game_tag,
        "version": manifest.version,
        "downloaded": len(todo),
        "skipped": len(manifest.files) - len(todo),
        "bytes": total,
        "files": [entry.path for entry in todo],
    }
