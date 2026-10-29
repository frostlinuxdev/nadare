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
import inspect
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from . import artindex, paths
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

# Stable publisher pages and their distinct image hosts. The OGP image URLs are
# intentionally resolved from page metadata because publishers rotate hashed paths.
# Pages are fetched without following redirects, so each entry names the final URL.
# These are social-share images (card-sized art), not heroes.
_PUBLISHER_OGP: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "direct-genshin": (
        "https://genshin.hoyoverse.com/en/home",
        ("genshin.hoyoverse.com",),
        ("upload-static.hoyoverse.com",),
    ),
    "direct-hsr": (
        "https://hsr.hoyoverse.com/en-us/",
        ("hsr.hoyoverse.com",),
        ("fastcdn.hoyoverse.com",),
    ),
    "direct-zzz": (
        "https://zenless.hoyoverse.com/en-us/",
        ("zenless.hoyoverse.com",),
        ("fastcdn.hoyoverse.com",),
    ),
    "direct-bd2": (
        "https://www.browndust2.com/en-us/",
        ("www.browndust2.com",),
        ("www.browndust2.com",),
    ),
    "direct-aethergazer": (
        "https://www.aethergazer.com/",
        ("www.aethergazer.com",),
        ("aethergazer.com", "www.aethergazer.com"),
    ),
    "direct-mongil": (
        "https://stardive.netmarble.com/en",
        ("stardive.netmarble.com",),
        ("sgimage.netmarble.com",),
    ),
    "launcher-nikke": (
        "https://nikke-en.com/",
        ("nikke-en.com",),
        ("nikke-en.com",),
    ),
    "direct-endfield": (
        "https://endfield.gryphline.com/en-us",
        ("endfield.gryphline.com",),
        ("web-static.hg-cdn.com",),
    ),
    # Arknights, Stella Sora and CZN are left out on purpose: Arknights' page advertises a dead
    # image, Stella Sora's is a favicon, and CZN's /en redirects to a rotating promo page (the
    # fetch never follows redirects), which would all be worse than the placeholder.
    "fgo-lepton": (
        "https://fate-go.us/",
        ("fate-go.us",),
        ("fate-go.us",),
    ),
}


#: Games whose page has no usable og:image but whose publisher hosts one stable share image.
_PUBLISHER_FIXED: dict[str, tuple[str, tuple[str, ...]]] = {
    "stove-czn": (
        "https://static-pubcomm.onstove.com/live/czn/brand/images/en/ogtag_1200_default.jpg",
        ("static-pubcomm.onstove.com",),
    ),
}

#: Games with no stable cover URL (Arknights' key visual is a hashed asset, Stella Sora's page
#: advertises a favicon): the newest image of their official news feed stands in.
_PUBLISHER_NEWS: frozenset[str] = frozenset({"arknights-yostar", "stellasora-yostar"})


#: HoYoverse games, whose backend class carries ``art_source`` (the launcher games feed: a cover
#: and a hero per game) and ``art_image_hosts``.
_PUBLISHER_HOYO: frozenset[str] = frozenset(
    {"direct-genshin", "direct-hsr", "direct-zzz", "direct-hi3"}
)


def publisher_art_kinds() -> frozenset[str]:
    """Backend kinds whose cover can come from their publisher."""
    return (
        frozenset(_PUBLISHER_OGP)
        | frozenset(_PUBLISHER_FIXED)
        | _PUBLISHER_NEWS
        | _PUBLISHER_HOYO
    )


def _publisher_image_hosts(backend: str) -> tuple[str, ...] | None:
    """Hosts an official cover of ``backend`` may be served from, or ``None``."""
    if backend in _PUBLISHER_HOYO:
        from . import backends

        feed_hosts = tuple(getattr(backends.get(backend), "art_image_hosts", ()) or ())
        page_hosts = _PUBLISHER_OGP[backend][2] if backend in _PUBLISHER_OGP else ()
        return tuple(dict.fromkeys((*feed_hosts, *page_hosts)))
    if backend in _PUBLISHER_OGP:
        return _PUBLISHER_OGP[backend][2]
    if backend in _PUBLISHER_FIXED:
        return _PUBLISHER_FIXED[backend][1]
    if backend in _PUBLISHER_NEWS:
        from . import backends

        return tuple(getattr(backends.get(backend), "official_image_hosts", ()) or ())
    return None


#: How many news images are tried, newest first, before the placeholder stays.
_NEWS_COVER_TRIES = 4


def _news_covers(backend: str) -> list[tuple[str, tuple[str, ...]]]:
    """Newest-first news images of ``backend`` that are https and on its official CDN."""
    from . import backends

    cls = backends.get(backend)
    source = getattr(cls, "news_source", None)
    hosts = tuple(getattr(cls, "official_image_hosts", ()) or ())
    if source is None or not hosts:
        return []
    try:
        items = source().items
    except Exception as exc:  # noqa: BLE001 - the news cover is an optional fallback
        log.debug("news cover lookup failed (%s): %s", backend, exc)
        return []
    found: list[tuple[str, tuple[str, ...]]] = []
    for item in items:
        image = str(item.get("image") or "").strip()
        if image and host_allowed(image, hosts) and _public_resolution(image):
            found.append((image, hosts))
            if len(found) == _NEWS_COVER_TRIES:
                break
    return found

SEARCH_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 60

#: Hard cap on a single image body; a hostile/oversized URL must not exhaust RAM.
MAX_IMAGE_BYTES = 16 * 1024 * 1024
#: Hard cap on a metadata/JSON response body (search/grid/GraphQL payloads).
MAX_JSON_BYTES = 4 * 1024 * 1024

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
            path = _migrated_cache_path(path)
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
        path = _migrated_cache_path(path)
        if path.is_file():
            return path
    return None


def _migrated_cache_path(path: Path) -> Path:
    """Resolve a saved pre-migration cache path to its WebP replacement."""
    if path.is_file() or path.parent != cache_dir():
        return path
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
        return path
    replacement = path.with_suffix(".webp")
    return replacement if replacement.is_file() else path


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


def _json_body(resp: Any, url: str) -> Any:
    """Parse a JSON response, capped when the client exposes a stream.

    Test doubles and older clients without ``iter_content`` keep using
    ``resp.json()``; the real ``curl_cffi`` stream is read through
    :func:`_read_capped` so a hostile body cannot exhaust memory.
    """
    if callable(getattr(resp, "iter_content", None)):
        return json.loads(_read_capped(resp, MAX_JSON_BYTES, url))
    return resp.json()


def _request_json(method: str, url: str, **kwargs):
    """Issue a JSON request without following redirects, then parse it."""
    call = _curl().get if method == "GET" else _curl().post
    resp = call(url, timeout=kwargs.pop("timeout"), allow_redirects=False, stream=True, **kwargs)
    try:
        if _status(resp) != 200:
            raise RuntimeError(f"HTTP {_status(resp)} for {url}")
        return _json_body(resp, url)
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = SEARCH_TIMEOUT):
    return _request_json("GET", url, timeout=timeout, headers=headers or {})


def _post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = SEARCH_TIMEOUT,
):
    return _request_json(
        "POST", url, timeout=timeout, json=payload, headers=headers or {}
    )


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


def _download(
    url: str, *, timeout: int = DOWNLOAD_TIMEOUT, cancel=None
) -> tuple[bytes, str]:
    # No redirects: the initial URL passed the host allowlist, and following a
    # 3xx could silently land on an internal/unlisted host (M2).
    if cancel is not None and cancel.cancelled:
        raise RuntimeError("cancelled")
    resp = _curl().get(url, timeout=timeout, allow_redirects=False, stream=True)
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

#: Resolver used by :func:`resolves_publicly`; tests replace this module global.
_RESOLVER: Any = socket.getaddrinfo
#: RFC 2606/6761 names that only exist in tests/documentation: a real resolver
#: cannot return a globally routable address for them, so skip the DNS check.
_RESERVED_TEST_SUFFIXES = (".example", ".test", ".invalid")


def resolves_publicly(host: str, resolver: Any = None) -> bool:
    """True only when *every* address ``host`` resolves to is globally routable.

    A public-looking name such as ``127.0.0.1.nip.io`` must not slip past an
    empty host allowlist. Resolution failure is treated as unsafe. DNS
    rebinding between this check and the connect is *not* covered.
    """
    if resolver is None:
        resolver = _RESOLVER
    try:
        infos = resolver(host, None)
    except (OSError, ValueError):
        return False
    found = False
    for info in infos:
        try:
            raw = info[4][0]
        except (IndexError, TypeError):
            return False
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            return False
        found = True
    return found


def _public_resolution(url: str) -> bool:
    """DNS gate for an empty-allowlist (third-party) URL."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or host.endswith(_RESERVED_TEST_SUFFIXES):
        return True
    return resolves_publicly(host)


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
    timeout: int = DOWNLOAD_TIMEOUT,
    cancel=None,
) -> Path | None:
    """Download and cache ``url`` as ``{slug}-{kind}.{ext}``.

    The single funnel for all image downloads: it enforces the per-backend
    host allowlist and swallows every failure so the UI degrades to a
    placeholder.  ``official`` marks publisher provenance, but is only honoured
    when ``allowed_hosts`` is non-empty: without a trust anchor the bytes are
    cached as third-party art rather than spoofed as official (M1).
    """
    try:
        if cancel is not None and cancel.cancelled:
            return None
        if not host_allowed(url, allowed_hosts):
            log.debug("image host not allowlisted: %s", url)
            return None
        # No trust anchor: refuse a name that resolves to loopback/private space.
        # (DNS rebinding between this check and the connect is not covered.)
        if not allowed_hosts and not _public_resolution(url):
            log.debug("image host does not resolve publicly: %s", url)
            return None
        if official and not allowed_hosts:
            log.debug("no allowlist; caching as third-party: %s", url)
            official = False
        return _store(
            url, slug, kind, official=official, timeout=timeout, cancel=cancel
        )
    except Exception as exc:  # noqa: BLE001 - a missing image is not fatal
        log.debug("image download failed (%s): %s", url, exc)
        return None


def download_compat(
    url: str,
    slug: str,
    kind: str,
    allowed_hosts: tuple[str, ...] = (),
    *,
    official: bool = True,
    timeout: int = DOWNLOAD_TIMEOUT,
    cancel=None,
) -> Path | None:
    """Call :func:`download`, passing timeout/cancel only when it accepts them.

    Tests and downstream users replace ``download`` with a small fake; those
    fakes should not have to grow new keyword arguments just because a caller
    wants a cancellable, short-timeout fetch.
    """
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(download).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callable
        params = {}
    if "official" in params:
        kwargs["official"] = official
    if "timeout" in params:
        kwargs["timeout"] = timeout
    if "cancel" in params:
        kwargs["cancel"] = cancel
    return download(url, slug, kind, allowed_hosts, **kwargs)


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


def _catalog_appid(profile: dict) -> int | None:
    """The Steam app id a catalog game ships with, if it has one."""
    from . import directkinds

    return directkinds.ART.get(str(profile.get("backend") or ""))


def _steam_candidates(profile: dict, kind: str = "cover") -> Iterator[str]:
    appid = profile.get("steam_appid")
    if not appid:
        appid = _catalog_appid(profile)
    if not appid:
        return
    for name in _STEAM_FILES.get(kind, _STEAM_FILES["cover"]):
        yield STEAM_CDN.format(appid=appid, name=name)


class _OgImageParser(HTMLParser):
    """Extract a publisher page's Open Graph image without a third-party parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._og: str | None = None
        self._twitter: str | None = None

    @property
    def image(self) -> str | None:
        """The Open Graph image, else the Twitter card image (some pages leave og empty)."""
        return self._og or self._twitter

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs}
        marker = (values.get("property") or values.get("name") or "").lower()
        content = (values.get("content") or "").strip()
        if marker in {"og:image", "og:image:url"} and not self._og:
            self._og = content or None
        elif marker == "twitter:image" and not self._twitter:
            self._twitter = content or None


def _hoyo_covers(backend: str, kind: str) -> list[tuple[str, tuple[str, ...]]]:
    """The launcher-feed image of ``kind`` ("cover" or "hero") for a HoYoverse game."""
    from . import backends

    cls = backends.get(backend)
    source = getattr(cls, "art_source", None)
    hosts = tuple(getattr(cls, "art_image_hosts", ()) or ())
    if source is None or not hosts:
        return []
    try:
        url = source().get(kind)
    except Exception as exc:  # noqa: BLE001 - the launcher feed is an optional source
        log.debug("HoYoverse art lookup failed (%s): %s", backend, exc)
        return []
    return [(url, hosts)] if url else []


def _publisher_covers(profile: dict, kind: str) -> list[tuple[str, tuple[str, ...]]]:
    """Every official cover URL worth trying for ``profile``, best first."""
    backend = str(profile.get("backend") or "")
    if backend in _PUBLISHER_HOYO and kind in ("cover", "hero"):
        found = _hoyo_covers(backend, kind)
        if kind == "cover":
            single = _publisher_og_image(profile, kind)  # the page image if the feed is down
            if single:
                found.append(single)
        return found
    if kind != "cover":
        return []
    if backend in _PUBLISHER_NEWS:
        return _news_covers(backend)
    single = _publisher_og_image(profile, kind)
    return [single] if single else []


def _publisher_og_image(profile: dict, kind: str) -> tuple[str, tuple[str, ...]] | None:
    """Resolve a catalog publisher's current cover URL from its official page."""
    if kind != "cover":
        return None
    backend = str(profile.get("backend") or "")
    if backend in _PUBLISHER_FIXED:
        return _PUBLISHER_FIXED[backend]
    config = _PUBLISHER_OGP.get(backend)
    if config is None:
        return None
    page, page_hosts, image_hosts = config
    if not host_allowed(page, page_hosts) or not _public_resolution(page):
        return None
    response = None
    try:
        response = _curl().get(
            page, timeout=SEARCH_TIMEOUT, allow_redirects=False, stream=True
        )
        if _status(response) != 200:
            return None
        body = _read_capped(response, MAX_JSON_BYTES, page)
        parser = _OgImageParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        if not parser.image:
            return None
        image = urljoin(page, parser.image.strip())
        if not host_allowed(image, image_hosts) or not _public_resolution(image):
            return None
        return image, image_hosts
    except Exception as exc:  # noqa: BLE001 - publisher metadata is an optional fallback
        log.debug("publisher cover lookup failed (%s): %s", page, exc)
        return None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _publisher_art_source(profile: dict, path: Path) -> bool:
    """Return whether a cached image has a validated publisher CDN source sidecar."""
    backend = str(profile.get("backend") or "")
    # Art the index prescribes is curated publisher art: its hosts are allowlisted per game in
    # code, even where they are a store CDN rather than the publisher's own host.
    publisher = _publisher_image_hosts(backend) or ()
    hosts = tuple(dict.fromkeys((*publisher, *artindex.ALLOWED_HOSTS.get(backend, ()))))
    if not hosts or path.parent != cache_dir():
        return False
    try:
        source = src_sidecar(path).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return host_allowed(source, hosts)


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


def _catalog_candidates(profile: dict, kind: str) -> Iterator[str]:
    yield from _steam_candidates(profile, kind)


def _optional_candidates(profile: dict, title: str | None) -> Iterator[str]:
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


def _candidates(profile: dict, title: str | None, kind: str = "cover") -> Iterator[str]:
    """Yield catalog art first, then optional SteamGridDB/AniList sources."""
    yield from _catalog_candidates(profile, kind)
    yield from _optional_candidates(profile, title)


# ---------------------------------------------------------------------------
# cache write + public entry point
# ---------------------------------------------------------------------------
def _cached(slug: str, kind: str) -> Path | None:
    root = cache_dir()
    root_key = str(root)
    if root.is_dir() and root_key not in _migrated_cache_roots:
        _schedule_cover_migration(root)
    # Prefer the replacement if migration has installed it but not yet removed
    # the original file.
    for ext in ("webp", *(ext for ext in _IMAGE_EXTS if ext != "webp")):
        candidate = root / f"{slug}-{kind}.{ext}"
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


#: Formats worth re-encoding; GIFs may be animated and WebP is already small.
_RECODE_EXTS = frozenset({"png", "jpg", "jpeg", "bmp"})
WEBP_QUALITY = 90
_migrated_cache_roots: set[str] = set()
_migrating_cache_roots: set[str] = set()
_migration_lock = threading.Lock()


def _schedule_cover_migration(root: Path) -> None:
    """Run the one-time cache migration off the caller thread (often the GUI)."""
    root_key = str(root)
    with _migration_lock:
        if root_key in _migrated_cache_roots or root_key in _migrating_cache_roots:
            return
        _migrating_cache_roots.add(root_key)

    def run() -> None:
        try:
            _cleanup_stale_cache_parts(root)
            migrate_cached_covers()
        finally:
            with _migration_lock:
                _migrating_cache_roots.discard(root_key)
                _migrated_cache_roots.add(root_key)

    try:
        threading.Thread(target=run, name="cover-cache-migration", daemon=True).start()
    except Exception:
        with _migration_lock:
            _migrating_cache_roots.discard(root_key)


def _recode_webp(content: bytes, ext: str) -> tuple[bytes, str]:
    """Re-encode a downloaded PNG/JPEG/BMP as WebP when that is smaller.

    Publisher art arrives as large PNGs; WebP keeps the picture (and any alpha) at a
    fraction of the size. Uses Qt's image writer when Qt is importable and its WebP
    plugin is present; on any failure the original bytes are returned untouched.
    """
    if ext not in _RECODE_EXTS:
        return content, ext
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtGui import QImage

        image = QImage.fromData(content)
        if image.isNull():
            return content, ext
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "WEBP", WEBP_QUALITY):
            return content, ext
        encoded = bytes(data)
    except Exception as exc:  # noqa: BLE001 - recoding is an optimisation only
        log.debug("webp recode skipped: %s", exc)
        return content, ext
    if not encoded or len(encoded) >= len(content):
        return content, ext
    return encoded, "webp"


def migrate_cached_covers() -> int:
    """Convert cached PNG/JPEG covers to WebP without risking originals.

    Normal lookups schedule this sweep on a daemon thread so decoding a large
    existing cache cannot block the GUI. Existing profile paths are resolved to
    the same-stem WebP by :func:`_migrated_cache_path` after conversion.
    """
    root = cache_dir()
    changed = 0
    try:
        sources = list(root.iterdir()) if root.is_dir() else []
    except OSError as exc:
        log.debug("cached cover migration could not scan %s: %s", root, exc)
        return 0
    for source in sources:
        if (
            not source.is_file()
            or source.suffix.lower().lstrip(".") not in {"png", "jpg", "jpeg"}
        ):
            continue
        try:
            original = source.read_bytes()
            encoded, ext = _recode_webp(original, source.suffix.lower().lstrip("."))
            if ext != "webp" or not encoded or encoded == original:
                continue
            from PyQt6.QtGui import QImage

            if QImage.fromData(encoded).isNull():
                continue
            target = source.with_suffix(".webp")
            if target.exists():
                continue
            part = _part_path(target)
            source_sidecar = src_sidecar(source)
            try:
                with open(part, "wb") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                # Verify the durable temporary file before installing it.
                if QImage.fromData(part.read_bytes()).isNull():
                    continue
                if source_sidecar.is_file():
                    _copy_sidecar_atomically(source_sidecar, src_sidecar(target))
                os.replace(part, target)
                _sync_dir(root)
                if news_marker(source).is_file():  # a news cover stays a news cover
                    news_marker(target).touch()
            finally:
                part.unlink(missing_ok=True)
            if source_sidecar.is_file():
                source_sidecar.unlink()
            news_marker(source).unlink(missing_ok=True)
            source.unlink()
            changed += 1
        except Exception as exc:  # noqa: BLE001 - cache migration must not break startup
            log.debug("cached cover migration skipped for %s: %s", source, exc)
    return changed


def _copy_sidecar_atomically(source: Path, target: Path) -> None:
    """Durably copy an official URL sidecar before its old image is removed."""
    data = source.read_bytes()
    part = _part_path(target)
    try:
        with open(part, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, target)
        _sync_dir(target.parent)
    finally:
        part.unlink(missing_ok=True)


def _part_owner_is_alive(path: Path) -> bool:
    """Protect a generated temp file while the process that owns it is alive."""
    pieces = path.name.rsplit(".", 3)
    if (
        len(pieces) != 4
        or pieces[3] != "part"
        or not re.fullmatch(r"[0-9a-f]{32}", pieces[2])
    ):
        return False
    try:
        pid = int(pieces[1])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cleanup_stale_cache_parts(root: Path) -> None:
    """Remove old regular ``.part`` files in the flat covers cache only."""
    cutoff = time.time() - 24 * 60 * 60
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.name.endswith(".part") or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff or _part_owner_is_alive(path):
                continue
            path.unlink()
        except OSError:
            continue


def _store(
    url: str,
    slug: str,
    kind: str,
    *,
    official: bool = False,
    timeout: int = DOWNLOAD_TIMEOUT,
    cancel=None,
) -> Path:
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
    content, ext = _download(url, timeout=timeout, cancel=cancel)
    content, ext = _recode_webp(content, ext)
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


def news_marker(path: Path | str) -> Path:
    """Sidecar that says a cover came from a news feed, so it carries a baked-in banner."""
    return Path(f"{path}.news")


def is_news_cover(path: Path | str) -> bool:
    return news_marker(path).is_file()


def _mark_news_cover(path: Path, backend: str, kind: str) -> None:
    """Record that ``path`` is a news image used as ``backend``'s cover (best effort)."""
    if kind != "cover" or backend not in _PUBLISHER_NEWS:
        return
    marker = news_marker(path)
    if marker.is_file():
        return  # already marked: no write on every cache hit
    try:
        marker.touch()
    except OSError as exc:
        log.debug("could not mark the news cover %s: %s", path, exc)


#: Index art URLs that failed to download, with when (monotonic), so a dead URL is not retried
#: on every fetch of every game.
_index_failures: dict[str, float] = {}
_INDEX_RETRY_AFTER = 3600.0


def _index_art(slug_: str, kind: str, backend: str, *, wait: bool) -> Path | None:
    """The cover the index prescribes for ``backend``, downloaded and cached, or ``None``.

    A cover already cached from the same URL is reused without any request. One cached from
    another source is replaced once the better art has downloaded; if that fails the cover that
    is already there stays. ``wait`` says whether to wait for the index itself (an explicit
    fetch, or nothing cached yet); otherwise the copy saved on disk answers at once.
    """
    if kind != "cover":
        return None
    for url, hosts in artindex.lookup(backend, wait=wait):
        hit = _cached_official(slug_, kind, url)
        if hit is not None:
            return hit
        failed_at = _index_failures.get(url)
        if failed_at is not None and time.monotonic() - failed_at < _INDEX_RETRY_AFTER:
            continue
        path = download(url, slug_, kind, hosts, official=True)
        if path is None:
            _index_failures[url] = time.monotonic()
            continue
        _drop_superseded(slug_, kind, path)
        return path
    return None


def _drop_superseded(slug_: str, kind: str, new: Path) -> None:
    """Clear what the new cover replaces: its news marker, and an older WebP that would shadow it.

    ``_cached`` prefers a ``.webp`` over every other type, so an older WebP left next to a newer
    PNG or JPEG would keep being shown. The new image is not a news banner either way.
    """
    news_marker(new).unlink(missing_ok=True)
    if new.suffix.lower() == ".webp":
        return
    old = cache_dir() / f"{slug_}-{kind}.webp"
    if old.is_file():
        for leftover in (old, src_sidecar(old), news_marker(old)):
            leftover.unlink(missing_ok=True)


def fetch(
    profile: dict,
    title: str | None = None,
    kind: str = "cover",
    *,
    wait_for_index: bool = False,
) -> Path | None:
    """Download the best available art for ``profile`` into the cache.

    Returns the cached path, reusing an existing file when present. Any failure
    (network, parse, disk) is logged and swallowed so callers get ``None``. With
    ``wait_for_index`` the curated index art is looked up even when a cover is already cached
    (an explicit fetch); otherwise a cached cover is returned without waiting for the network.
    """
    try:
        slug_ = slug(profile, title)
        cached = _cached(slug_, kind)
        pinned = _index_art(
            slug_,
            kind,
            str(profile.get("backend") or ""),
            wait=wait_for_index or cached is None,
        )
        if pinned is not None:
            return pinned
        if cached is not None:
            log.debug("artwork cache hit: %s", cached)
            backend = str(profile.get("backend") or "")
            # These games have no cover but a news image, which is how an official sidecar got
            # here; covers cached before the marker existed are marked on first use.
            if src_sidecar(cached).is_file():
                _mark_news_cover(cached, backend, kind)
            return cached

        def try_candidates(urls: Iterator[str]) -> Path | None:
            for url in urls:
                if not host_allowed(url, ()):  # https only, never a local/private host
                    log.debug("artwork candidate rejected (host): %s", url)
                    continue
                if not _public_resolution(url):
                    # Public-looking name that actually resolves locally (#32.4).
                    log.debug("artwork candidate rejected (resolves locally): %s", url)
                    continue
                try:
                    return _store(url, slug_, kind)
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    log.debug("artwork candidate failed (%s): %s", url, exc)
            return None

        path = try_candidates(_catalog_candidates(profile, kind))
        if path is not None:
            return path
        for url, allowed_hosts in _publisher_covers(profile, kind):
            path = download(url, slug_, kind, allowed_hosts, official=True)
            if path is not None:
                _mark_news_cover(path, str(profile.get("backend") or ""), kind)
                return path
        path = try_candidates(_optional_candidates(profile, title))
        if path is not None:
            return path
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
        path = fetch(profile, title=title, kind=kind, wait_for_index=True)
        if path is not None and _publisher_art_source(profile, path):
            if record_official(profile, path, kind):
                found[kind] = path
        elif path is not None and record_thirdparty(profile, path, kind):
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
    "download_compat",
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
    "resolves_publicly",
    "set_art_source",
    "slug",
    "source_of",
    "src_sidecar",
]
