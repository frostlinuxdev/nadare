# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Optional, cached artwork fetcher.

A profile points at local art through a handful of keys (see ``docs/ARTWORK.md``).
When it has none, this module can look the art up online, download it once and
cache it under ``$XDG_DATA_HOME/nadare/covers`` so later runs are offline.

Sources are tried in order of specificity:

1. **Steam CDN** — when the profile has a ``steam_appid`` (keyless).
2. **SteamGridDB** — only when the user provides a key (``SGDB_API_KEY`` or
   the profile/config ``sgdb_api_key``); the old keyless endpoint is gone.
3. **AniList GraphQL** — opt-in only, via ``NADARE_ART_THIRD_PARTY=1``.

Official publisher art (e.g. a CZN news hero) does not go through this source
list: it is downloaded through :func:`download` with a per-backend host
allowlist, so third-party scraping is never used by default.

Nothing here ever raises into the UI: :func:`fetch` logs a failure and returns
``None`` so a missing cover degrades to the deterministic placeholder.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from . import paths
from .logging import get_logger

log = get_logger("artwork")

#: Profile keys that may hold a local image, in display priority order.
ART_KEYS = ("cover", "hero", "thumb", "artwork", "custom_background")

#: Provenance recorded under the profile's ``art_source`` mapping.
SOURCE_USER = "user"
SOURCE_OFFICIAL = "official"
SOURCE_THIRDPARTY = "thirdparty"

#: Lower tier wins: user art beats official art, which beats third-party art.
SOURCE_TIER = {SOURCE_USER: 0, SOURCE_OFFICIAL: 1, SOURCE_THIRDPARTY: 2}

#: Files the old fetcher dropped in the cover cache (``*-cover.*`` etc.).
_FETCHED_SUFFIX = re.compile(r".+-(?:cover|hero|logo|icon|thumb)\.[A-Za-z0-9]+$")

STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{name}"
_STEAM_FILES: dict[str, tuple[str, ...]] = {
    "cover": ("library_600x900.jpg", "header.jpg", "hero.jpg"),
    "thumb": ("library_600x900.jpg", "header.jpg"),
    "hero": ("hero.jpg", "header.jpg"),
    "logo": ("logo.png", "header.jpg"),
    "icon": ("logo.png",),
}

SGDB_API = "https://www.steamgriddb.com/api/v2"
ANILIST_API = "https://graphql.anilist.co"
ANILIST_QUERY = (
    "query ($search: String) { Media(search: $search, type: ANIME) "
    "{ coverImage { extraLarge large } } }"
)

SEARCH_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 60

#: Hard cap on a single image body; a hostile/oversized URL must not exhaust RAM.
MAX_IMAGE_BYTES = 16 * 1024 * 1024

_IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "bmp")
_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}


# ---------------------------------------------------------------------------
# local art + cache locations
# ---------------------------------------------------------------------------
def art_source(profile: dict) -> dict[str, str]:
    """Return the profile's ``art_source`` mapping (possibly empty)."""
    raw = profile.get("art_source") if isinstance(profile, dict) else None
    if not isinstance(raw, dict):
        extra = profile.get("extra") if isinstance(profile, dict) else None
        raw = extra.get("art_source") if isinstance(extra, dict) else None
    return raw if isinstance(raw, dict) else {}


def set_art_source(profile: dict, key: str, source: str) -> None:
    """Record where ``key``'s image came from, creating the mapping as needed."""
    mapping = profile.setdefault("art_source", {})
    if not isinstance(mapping, dict):
        mapping = {}
        profile["art_source"] = mapping
    mapping[key] = source


def source_of(profile: dict, key: str, value: Any) -> str:
    """Provenance of ``profile[key]``.

    Explicit ``art_source`` wins.  A ``custom_background`` chosen through
    Settings is the user's.  Anything else (including a legacy cached file the
    old fetcher wrote) is unknown and treated as third-party, so official art
    may replace it but user art may not.
    """
    declared = art_source(profile).get(key)
    if declared in SOURCE_TIER:
        return declared
    if key == "custom_background":
        return SOURCE_USER
    try:
        path = Path(str(value)).expanduser()
    except (TypeError, ValueError):
        return SOURCE_THIRDPARTY
    if path.parent == cache_dir() and _FETCHED_SUFFIX.match(path.name):
        return SOURCE_THIRDPARTY
    return SOURCE_THIRDPARTY


def _entries(profile: dict, keys: tuple[str, ...]) -> list[tuple[int, int, int, Path]]:
    """Existing images as ``(tier, source_order, key_order, path)`` tuples."""
    sources = [profile]
    extra = profile.get("extra") if isinstance(profile, dict) else None
    if isinstance(extra, dict):
        sources.append(extra)
    found: list[tuple[int, int, int, Path]] = []
    for source_index, source in enumerate(sources):
        for key_index, key in enumerate(keys):
            value = source.get(key)
            if not value:
                continue
            try:
                path = Path(str(value)).expanduser()
            except (TypeError, ValueError):
                continue
            if not path.is_file():
                continue
            tier = SOURCE_TIER[source_of(profile, key, value)]
            found.append((tier, source_index, key_index, path))
    return found


def resolve(profile: dict) -> Path | None:
    """Return the best existing local image for ``profile``.

    The list is ordered by provenance first (user > official > third-party),
    then by the :data:`ART_KEYS` priority, so an official image is never hidden
    by a lower-priority third-party one.  Returns ``None`` when nothing on disk
    matches.
    """
    entries = _entries(profile, ART_KEYS)
    if not entries:
        return None
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return entries[0][3]


def resolve_kind(profile: dict, kind: str) -> Path | None:
    """Return the existing file for one specific art ``kind``, if any."""
    key = KIND_KEYS.get(kind, kind)
    for source in (profile, profile.get("extra") or {}):
        if not isinstance(source, dict):
            continue
        value = source.get(key)
        if not value:
            continue
        try:
            path = Path(str(value)).expanduser()
        except (TypeError, ValueError):
            continue
        if path.is_file():
            return path
    return None


def ordered_paths(profile: dict, keys: tuple[str, ...] | None = None) -> list[str]:
    """Existing image paths for ``keys``, best (user > official > third-party) first."""
    entries = _entries(profile, keys or ART_KEYS)
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    out: list[str] = []
    for _tier, _source, _key, path in entries:
        text = str(path)
        if text not in out:
            out.append(text)
    return out


def record(profile: dict, path: Path | str, kind: str, source: str) -> bool:
    """Record a fetched image, honouring provenance precedence.

    An ``official`` record overwrites third-party / unknown entries but never a
    ``user`` one; a ``thirdparty`` record never overwrites official or user art.
    Official provenance is only granted when the file carries the source-URL
    sidecar written by :func:`download`, so a stale third-party cache file can
    never be relabelled as publisher art.
    Returns ``True`` when the profile was updated.
    """
    if source == SOURCE_OFFICIAL and not is_official_file(path):
        return False
    key = KIND_KEYS.get(kind, kind)
    declared = art_source(profile).get(key)
    if declared is not None and declared not in SOURCE_TIER:
        declared = None
    if declared == SOURCE_USER:
        return False
    if source != SOURCE_OFFICIAL and declared == SOURCE_OFFICIAL:
        return False
    profile[key] = str(path)
    set_art_source(profile, key, source)
    return True


def record_official(profile: dict, path: Path | str, kind: str = "hero") -> bool:
    """Record publisher art (never clobbers a user choice)."""
    return record(profile, path, kind, SOURCE_OFFICIAL)


def record_thirdparty(profile: dict, path: Path | str, kind: str = "cover") -> bool:
    """Record scraped/third-party art (never clobbers user or official art)."""
    return record(profile, path, kind, SOURCE_THIRDPARTY)


def cache_dir() -> Path:
    """The shared cover cache: ``$XDG_DATA_HOME/nadare/covers``."""
    return paths.data_dir() / "covers"


def src_sidecar(path: Path | str) -> Path:
    """The ``{file}.src`` sidecar holding an official image's source URL."""
    image = Path(str(path))
    return image.with_name(image.name + ".src")


def is_official_file(path: Path | str) -> bool:
    """True when ``path`` is a cache file written by the official fetch.

    Only files under :func:`cache_dir` carrying a source-URL sidecar qualify, so
    a pre-existing third-party image (even one named ``*-official-*``) is never
    mistaken for publisher art.
    """
    try:
        image = Path(str(path))
    except (TypeError, ValueError):
        return False
    try:
        if image.parent != cache_dir():
            return False
    except (OSError, TypeError):
        return False
    return src_sidecar(image).is_file()


def apply(profile: dict, path: Path | str) -> dict:
    """Record a user-chosen cover on a mutable profile and return it."""
    profile["cover"] = str(path)
    set_art_source(profile, "cover", SOURCE_USER)
    return profile


# ---------------------------------------------------------------------------
# HTTP (curl_cffi, imported lazily so importing this module stays cheap)
# ---------------------------------------------------------------------------
def _curl():
    """Return the ``curl_cffi.requests`` module.

    Split out so tests can substitute a fake without the network.
    """
    from curl_cffi import requests

    return requests


def _status(resp) -> int:
    return int(getattr(resp, "status_code", 200) or 200)


def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = SEARCH_TIMEOUT):
    resp = _curl().get(url, timeout=timeout, headers=headers or {})
    if _status(resp) != 200:
        raise RuntimeError(f"HTTP {_status(resp)} for {url}")
    return resp.json()


def _post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = SEARCH_TIMEOUT,
):
    resp = _curl().post(url, timeout=timeout, json=payload, headers=headers or {})
    if _status(resp) != 200:
        raise RuntimeError(f"HTTP {_status(resp)} for {url}")
    return resp.json()


def _guess_ext(url: str, headers) -> str:
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix == "jpeg":
        return "jpg"
    if suffix in _IMAGE_EXTS:
        return suffix
    content_type = ""
    if headers is not None:
        try:
            content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
        except AttributeError:
            content_type = ""
    return _EXT_BY_TYPE.get(content_type.split(";")[0].strip().lower(), "jpg")


def _content_length(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Content-Length") or headers.get("content-length")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _read_capped(resp: Any, cap: int, url: str) -> bytes:
    """Read a response body, aborting once it would exceed ``cap`` bytes."""
    declared = _content_length(getattr(resp, "headers", None))
    if declared is not None and declared > cap:
        raise RuntimeError(f"image too large ({declared} bytes) for {url}")
    chunks = getattr(resp, "iter_content", None)
    if callable(chunks):
        buf = bytearray()
        for chunk in chunks(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > cap:
                raise RuntimeError(f"image too large for {url}")
        return bytes(buf)
    content = getattr(resp, "content", b"") or b""
    if len(content) > cap:
        raise RuntimeError(f"image too large for {url}")
    return content


def _download(url: str) -> tuple[bytes, str]:
    # No redirects: the initial URL passed the host allowlist, and following a
    # 3xx could silently land on an internal/unlisted host (M2).
    resp = _curl().get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False, stream=True)
    try:
        if _status(resp) != 200:
            raise RuntimeError(f"HTTP {_status(resp)} for {url}")
        content = _read_capped(resp, MAX_IMAGE_BYTES, url)
        if not content:
            raise RuntimeError(f"empty image body for {url}")
        return content, _guess_ext(url, getattr(resp, "headers", None))
    finally:
        # A streamed response holds its socket until closed; the GUI process is
        # long-lived and fetches many banners.
        close = getattr(resp, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def host_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """True when ``url`` is an https URL on an allowlisted host.

    An empty allowlist is permissive *by host* (used for caller-validated
    publisher media), but never for loopback/private/link-local addresses, so a
    fetched URL cannot be steered at the LAN. Official publisher images always
    pass a non-empty allowlist. Userinfo, an explicit port and non-https schemes
    are always rejected.
    """
    if not isinstance(url, str) or not url:
        return False
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.port is not None:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if not allowed_hosts:
        return not _is_local_host(host)
    return host in {name.lower() for name in allowed_hosts}


_NUMERIC_HOST_RE = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*")


def _is_local_host(host: str) -> bool:
    """True for loopback/private/link-local addresses and bare hostnames."""
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host.endswith((".local", ".internal", ".home.arpa")):
        return True
    if _NUMERIC_HOST_RE.fullmatch(host):
        # 127.1, 0x7f.0.0.1, 0177.0.0.1, 2130706433: normalise the way the
        # resolver would; anything inet_aton cannot parse is treated as local.
        try:
            host = socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a bare hostname is not a public FQDN
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global


def download(
    url: str,
    slug: str,
    kind: str,
    allowed_hosts: tuple[str, ...] = (),
    *,
    official: bool = True,
) -> Path | None:
    """Download and cache ``url`` as ``{slug}-{kind}.{ext}``.

    The single funnel for all image downloads: it enforces the per-backend
    host allowlist and swallows every failure so the UI degrades to a
    placeholder.  ``official`` marks publisher provenance, but is only honoured
    when ``allowed_hosts`` is non-empty: without a trust anchor the bytes are
    cached as third-party art rather than spoofed as official (M1).
    """
    try:
        if not host_allowed(url, allowed_hosts):
            log.debug("image host not allowlisted: %s", url)
            return None
        if official and not allowed_hosts:
            log.debug("no allowlist; caching as third-party: %s", url)
            official = False
        return _store(url, slug, kind, official=official)
    except Exception as exc:  # noqa: BLE001 - a missing image is not fatal
        log.debug("image download failed (%s): %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def slug(profile: dict, title: str | None = None) -> str:
    """A stable filename slug for a profile's cached art."""
    source = (
        title
        or profile.get("title")
        or profile.get("sgdb")
        or profile.get("anilist")
        or profile.get("name")
        or "artwork"
    )
    slug = re.sub(r"[^a-z0-9]+", "-", str(source).lower()).strip("-")
    return slug or "artwork"


def _sgdb_key(profile: dict) -> str | None:
    key = os.environ.get("SGDB_API_KEY") or profile.get("sgdb_api_key")
    if not key:
        extra = profile.get("extra")
        if isinstance(extra, dict):
            key = extra.get("sgdb_api_key")
    if not key:
        try:
            from .config import Config

            key = Config.load().data.get("sgdb_api_key")
        except Exception:  # noqa: BLE001 - config is best-effort here
            key = None
    return str(key) if key else None


def _steam_candidates(profile: dict, kind: str = "cover") -> Iterator[str]:
    appid = profile.get("steam_appid")
    if not appid:
        return
    for name in _STEAM_FILES.get(kind, _STEAM_FILES["cover"]):
        yield STEAM_CDN.format(appid=appid, name=name)


def _norm_title(text: object) -> str:
    """Casefold ``text`` and drop everything that is not alphanumeric.

    So ``"FATE / GRAND ORDER"``, ``"Fate/Grand Order"`` and ``"fategrandorder"``
    all compare equal. Unicode letters/digits are kept via :meth:`str.isalnum`.
    """
    return "".join(ch for ch in str(text).casefold() if ch.isalnum())


def _sgdb_candidates(profile: dict, title: str | None, key: str) -> Iterator[str]:
    """Yield SteamGridDB grid urls for one game.

    Only an exact title match is accepted (after :func:`_norm_title`): the search
    autocomplete often lists a spinoff first, so the first hit is not trusted.
    Set the profile's ``sgdb_id`` (int or digit string) to pin a game and skip the
    search entirely.
    """
    headers = {"Authorization": f"Bearer {key}"}

    pinned = profile.get("sgdb_id")
    if pinned is not None and str(pinned).strip().isdigit():
        game_id: object = str(pinned).strip()
    else:
        term = str(profile.get("sgdb") or title or profile.get("title") or "").strip()
        if not term:
            return
        search = _get_json(f"{SGDB_API}/search/autocomplete/{quote(term)}", headers=headers)
        games = search.get("data") if isinstance(search, dict) else None
        if not games:
            return
        wanted = _norm_title(term)
        games = [g for g in games if isinstance(g, dict)]
        match = next((g for g in games if _norm_title(g.get("name", "")) == wanted), None)
        if match is None:
            log.debug("SteamGridDB: no exact title match for %r", term)
            return
        game_id = match.get("id")
    if game_id is None:
        return
    grids = _get_json(f"{SGDB_API}/grids/game/{game_id}", headers=headers)
    for grid in (grids.get("data") if isinstance(grids, dict) else None) or []:
        url = grid.get("url") if isinstance(grid, dict) else None
        if url:
            yield str(url)


def _anilist_candidates(term: str) -> Iterator[str]:
    data = _post_json(ANILIST_API, {"query": ANILIST_QUERY, "variables": {"search": term}})
    media = (data.get("data") or {}).get("Media") if isinstance(data, dict) else None
    cover = (media or {}).get("coverImage") or {}
    for key in ("extraLarge", "large"):
        url = cover.get(key)
        if url:
            yield str(url)


def _candidates(profile: dict, title: str | None, kind: str = "cover") -> Iterator[str]:
    """Yield image URLs from the usable sources, best first, lazily.

    Steam CDN (publisher-supplied store art) always applies. SteamGridDB is only
    queried when an API key is present; AniList only when the user opts in with
    ``NADARE_ART_THIRD_PARTY=1``. Third-party scraping is off by default.
    """
    yield from _steam_candidates(profile, kind)

    key = _sgdb_key(profile)
    if key:
        try:
            yield from _sgdb_candidates(profile, title, key)
        except Exception as exc:  # noqa: BLE001 - fall through to the next source
            log.debug("SteamGridDB lookup failed: %s", exc)

    if os.environ.get("NADARE_ART_THIRD_PARTY") == "1":
        term = profile.get("anilist") or title or profile.get("title")
        if term:
            try:
                yield from _anilist_candidates(str(term))
            except Exception as exc:  # noqa: BLE001 - no source left
                log.debug("AniList lookup failed: %s", exc)


# ---------------------------------------------------------------------------
# cache write + public entry point
# ---------------------------------------------------------------------------
def _cached(slug: str, kind: str) -> Path | None:
    for ext in _IMAGE_EXTS:
        candidate = cache_dir() / f"{slug}-{kind}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def cached(slug: str, kind: str) -> Path | None:
    """Return an already-cached ``{slug}-{kind}`` image, if any."""
    return _cached(slug, kind)


def cached_art(profile: dict, title: str | None = None, kind: str = "hero") -> Path | None:
    """Return a cached image for ``profile`` without touching the network."""
    return _cached(slug(profile, title), kind)


#: A single, safe filename component: no separators, control chars or ``..``.
_SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def _safe_name(value: str, field: str) -> str:
    """Reject a caller/network-supplied filename component that could escape.

    ``slug`` and ``kind`` are interpolated into a path under :func:`cache_dir`,
    so a value containing ``/``, ``\\``, ``..`` or control characters must
    never be allowed to redirect the write outside the cache.
    """
    if not isinstance(value, str) or not _SAFE_NAME_RE.match(value) or ".." in value:
        raise ValueError(f"unsafe artwork {field}: {value!r}")
    return value


def _part_path(dest: Path) -> Path:
    """A unique sibling temp path so concurrent writers cannot collide."""
    return dest.with_name(f"{dest.name}.{os.getpid()}.{uuid.uuid4().hex}.part")


def _sync_dir(path: Path) -> None:
    """Best-effort ``fsync`` of a directory so a rename survives a power loss."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _store(url: str, slug: str, kind: str, *, official: bool = False) -> Path:
    """Write one downloaded image, keyed by URL when ``official``.

    Third-party images keep the legacy ``{slug}-{kind}.{ext}`` name and are
    reused whenever the file exists.  Official images are written under the
    caller-supplied name (``{slug}-official-hero`` etc.) and are only reused
    while their ``{file}.src`` sidecar matches ``url``.  ``slug`` and ``kind``
    are validated as single filename components so a network-supplied value
    can never redirect the write outside :func:`cache_dir`.
    """
    safe_slug = _safe_name(slug, "slug")
    safe_kind = _safe_name(kind, "kind")
    if official:
        cached_official = _cached_official(safe_slug, safe_kind, url)
        if cached_official is not None:
            return cached_official
    content, ext = _download(url)
    dest = cache_dir() / f"{safe_slug}-{safe_kind}.{ext}"
    try:
        if dest.resolve().parent != cache_dir().resolve():
            raise ValueError(f"artwork path escapes the cache: {dest}")
    except OSError as exc:  # pragma: no cover - resolve is non-strict
        raise ValueError(f"artwork path escapes the cache: {dest}") from exc
    if not official and dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(dest)
    try:
        with open(part, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, dest)
        _sync_dir(dest.parent)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if official:
        try:
            src_sidecar(dest).write_text(str(url), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk failure edge case
            log.debug("could not write art source sidecar: %s", exc)
    log.info("cached artwork %s (%d bytes)", dest, len(content))
    return dest


def _cached_official(slug: str, kind: str, url: str) -> Path | None:
    """Return a cached official image whose sidecar matches ``url``."""
    for ext in _IMAGE_EXTS:
        candidate = cache_dir() / f"{slug}-{kind}.{ext}"
        if not candidate.is_file():
            continue
        try:
            stored = src_sidecar(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if stored == url:
            return candidate
    return None


def fetch(profile: dict, title: str | None = None, kind: str = "cover") -> Path | None:
    """Download the best available art for ``profile`` into the cache.

    Returns the cached path, reusing an existing file when present. Any failure
    (network, parse, disk) is logged and swallowed so callers get ``None``.
    """
    try:
        slug_ = slug(profile, title)
        cached = _cached(slug_, kind)
        if cached is not None:
            log.debug("artwork cache hit: %s", cached)
            return cached
        for url in _candidates(profile, title, kind):
            if not host_allowed(url, ()):  # https only, never a local/private host
                log.debug("artwork candidate rejected (host): %s", url)
                continue
            try:
                return _store(url, slug_, kind)
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                log.debug("artwork candidate failed (%s): %s", url, exc)
        log.info("no artwork found for %s", slug_)
        return None
    except Exception as exc:  # noqa: BLE001 - never raise into the UI
        log.warning("artwork fetch failed: %s", exc)
        return None


#: Profile key each fetched kind is recorded under.
KIND_KEYS = {"cover": "cover", "hero": "hero", "logo": "logo", "icon": "icon"}


def fetch_all(
    profile: dict, title: str | None = None, kinds: tuple[str, ...] = ("cover", "hero", "logo")
) -> dict[str, Path]:
    """Fetch several kinds and record each on the profile.

    Fetched art is third-party by provenance, so it never overwrites a user
    image or an official one already recorded.  Like :func:`fetch`, it never
    raises.
    """
    found: dict[str, Path] = {}
    for kind in kinds:
        path = fetch(profile, title=title, kind=kind)
        if path is not None and record_thirdparty(profile, path, kind):
            found[kind] = path
    return found


__all__ = [
    "ART_KEYS",
    "SOURCE_USER",
    "SOURCE_OFFICIAL",
    "SOURCE_THIRDPARTY",
    "apply",
    "art_source",
    "cache_dir",
    "cached",
    "cached_art",
    "download",
    "fetch",
    "fetch_all",
    "host_allowed",
    "is_official_file",
    "ordered_paths",
    "record",
    "record_official",
    "record_thirdparty",
    "resolve",
    "resolve_kind",
    "set_art_source",
    "slug",
    "source_of",
    "src_sidecar",
]
