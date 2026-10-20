# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generic publisher-news fetcher, cache and image localiser.

A backend may expose a ``news_source`` callable (see
:class:`nadare.core.backends.GameBackend`) returning a :class:`NewsResult`.
This module owns the shared plumbing:

* :func:`http_get_json` — a small, impersonation-free JSON GET with a response
  body cap and no redirects, for public publisher APIs.
* :func:`fetch` — calls the source, normalises items, and caches the result as
  JSON under ``$XDG_CACHE_HOME/nadare/news/<key>.json`` with a TTL. When the
  source fails but a cache entry exists, the stale entry is served instead.
* :func:`load` — fetches, then downloads the official thumbnails/hero through
  the one allowlisted artwork helper and returns a UI-ready feed.

Nothing here ever raises into the UI: :func:`load` degrades to an unavailable
:class:`NewsResult`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from . import paths
from .logging import get_logger

log = get_logger("news")

#: Shown under the news panel.
CREDIT = "Artwork and news © their publishers. Nadare is not affiliated."

CACHE_TTL = 15 * 60
TIMEOUT = 12
MAX_BODY = 2 * 1024 * 1024
USER_AGENT = f"Nadare/{__version__}"
MAX_NEWS_IMAGES = 8


class NewsError(Exception):
    """A news source failed (network, bad envelope, no boards)."""


@dataclass
class NewsResult:
    """A normalised news feed plus fetch metadata."""

    items: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    stale: bool = False
    available: bool = True
    hero: str | None = None
    credit: str = CREDIT


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _curl():
    """Return ``curl_cffi.requests``; split out so tests can substitute it."""
    from curl_cffi import requests

    return requests


def http_get_json(
    url: str, *, headers: dict[str, str] | None = None, timeout: int = TIMEOUT
) -> Any:
    """GET ``url`` as JSON without browser impersonation or redirects.

    Raises :class:`NewsError` on a non-200 status, an oversized body or invalid
    JSON. The response body is capped at :data:`MAX_BODY` bytes *while reading*,
    so an oversized response never lands in memory whole.
    """
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged.update(headers)
    resp = _curl().get(
        url, timeout=timeout, headers=merged, allow_redirects=False, stream=True
    )
    try:
        status = int(getattr(resp, "status_code", 200) or 200)
        if status != 200:
            raise NewsError(f"HTTP {status} for {url}")
        body = _read_capped(resp, MAX_BODY, url)
        try:
            return json.loads(body) if body else resp.json()
        except (ValueError, TypeError) as exc:
            raise NewsError(f"invalid JSON for {url}") from exc
    finally:
        # ``stream=True`` holds the connection open until the body is consumed
        # (or the response is closed), so always release it rather than waiting
        # for GC on a long-lived GUI process.
        close = getattr(resp, "close", None)
        if callable(close):
            close()


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
        raise NewsError(f"response body too large for {url}")
    chunks = getattr(resp, "iter_content", None)
    if callable(chunks):
        buf = bytearray()
        for chunk in chunks(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > cap:
                raise NewsError(f"response body too large for {url}")
        return bytes(buf)
    body = getattr(resp, "content", b"") or b""
    if len(body) > cap:
        raise NewsError(f"response body too large for {url}")
    return body


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
def normalize_item(raw: Any) -> dict[str, Any] | None:
    """Coerce one raw item into the stable feed shape, or ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not item_id or not title or not url:
        return None
    image = raw.get("image")
    image = str(image).strip() if image else None
    try:
        published = int(raw.get("published_at") or 0)
    except (TypeError, ValueError):
        published = 0
    return {
        "id": item_id,
        "title": title[:300],
        "summary": str(raw.get("summary") or "")[:600],
        "category": str(raw.get("category") or ""),
        "published_at": published,
        "image": image or None,
        "url": url,
    }


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
#: A single safe filename component: no separators, control chars or ``..``.
_SAFE_KEY_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def cache_path(key: str) -> Path:
    """``$XDG_CACHE_HOME/nadare/news/<key>.json``.

    A non-constant/hostile ``key`` (one carrying ``/`` or ``..``) is hashed
    instead of interpolated, so it can never read or write outside the cache.
    """
    text = str(key)
    if not _SAFE_KEY_RE.match(text) or ".." in text:
        text = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return paths.cache_dir() / "news" / f"{text}.json"


def read_cache(key: str) -> dict[str, Any] | None:
    """Return the parsed cache entry, or ``None`` when missing/corrupt."""
    path = cache_path(key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data


def write_cache(key: str, result: NewsResult) -> None:
    """Persist a successful fetch; failures are logged, never raised."""
    path = cache_path(key)
    payload = {
        "fetched_at": int(time.time() * 1000),
        "partial": bool(result.partial),
        "items": result.items,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A per-writer temp name keeps two concurrent refreshes for one key from
        # truncating each other's ``.part`` file.
        part = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
        part.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(part, path)
    except OSError as exc:
        log.debug("could not cache news %s: %s", key, exc)


def _is_fresh(entry: dict[str, Any], ttl: int, now_ms: int) -> bool:
    try:
        fetched = int(entry.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return False
    age = now_ms - fetched
    return 0 <= age < ttl * 1000  # a future timestamp is stale, not eternally fresh


def _normalise(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items or []:
        item = normalize_item(raw)
        if item is not None:
            out.append(item)
    return out


def fetch(
    source: Callable[[], Any],
    *,
    key: str,
    ttl: int = CACHE_TTL,
    now_ms: int | None = None,
) -> NewsResult:
    """Fetch and cache a source's feed, falling back to a stale cache offline."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    cached = read_cache(key)
    if cached is not None and _is_fresh(cached, ttl, now):
        return NewsResult(
            items=_normalise(cached["items"]),
            partial=bool(cached.get("partial")),
        )

    try:
        raw = source()
    except Exception as exc:  # noqa: BLE001 - offline/parse failures are expected
        if cached is not None:
            log.info("news %s offline; serving stale cache", key)
            return NewsResult(
                items=_normalise(cached["items"]),
                partial=bool(cached.get("partial")),
                stale=True,
            )
        raise NewsError(str(exc)) from exc

    items = _normalise(getattr(raw, "items", raw))
    partial = bool(getattr(raw, "partial", False))
    result = NewsResult(items=items, partial=partial)
    write_cache(key, result)
    return result


# ---------------------------------------------------------------------------
# image localisation + UI-ready load
# ---------------------------------------------------------------------------
def _slug(key: str) -> str:
    return "news-" + key


def _item_slug(item_id: str) -> str:
    """A stable, filename-safe digest of a publisher-supplied news id.

    The ``id`` comes straight from the feed and must never reach the
    filesystem verbatim: hashing keeps the cached name deterministic without
    letting a hostile ``../../`` escape :func:`artwork.cache_dir`.
    """
    raw = str(item_id).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:16]


def _download_image(url: str, slug: str, kind: str, hosts: tuple[str, ...]):
    from . import artwork

    try:
        return artwork.download(url, slug, kind, hosts)
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail is not fatal
        log.debug("news image failed (%s): %s", url, exc)
        return None


def _hero_index(
    items: list[dict[str, Any]],
    categories: tuple[str, ...],
    hero_key: Callable[[dict[str, Any]], Any] | None = None,
) -> int | None:
    """Index of the item to use as the hero, or ``None``.

    When ``hero_key`` is set it ranks the candidates: the item with the
    greatest non-``None`` key wins (a backend hook for feeds whose display
    order, like FGO's weekly re-dated tips page, is not their event order).
    Otherwise a non-empty ``categories`` prefers the newest (first) item whose
    category matches; when nothing matches it falls back to the newest item
    with an image, exactly like the empty-categories case.
    """
    if hero_key is not None:
        ranked: list[tuple[Any, int]] = []
        for index, item in enumerate(items):
            if not item.get("image"):
                continue
            key = hero_key(item)
            if key is None:
                continue
            ranked.append((key, -index))
        if ranked:
            return -max(ranked)[1]
        return None
    if categories:
        allowed = set(categories)
        for index, item in enumerate(items):
            if item.get("image") and str(item.get("category") or "") in allowed:
                return index
    for index, item in enumerate(items):
        if item.get("image"):
            return index
    return None


def load(
    source: Callable[[], Any],
    *,
    key: str,
    hosts: tuple[str, ...] = (),
    slug: str | None = None,
    ttl: int = CACHE_TTL,
    now_ms: int | None = None,
    hero_categories: tuple[str, ...] = (),
    hero_key: Callable[[dict[str, Any]], Any] | None = None,
) -> NewsResult:
    """Return a UI-ready feed with local thumbnail/hero paths.

    Never raises: an unavailable source yields ``available=False``. The hero is
    the newest item with an image, unless ``hero_categories`` is set, in which
    case the newest item in one of those categories wins (falling back to the
    newest with an image when none match). A ``hero_key`` callable overrides
    both: the item with the greatest non-``None`` key wins. It is cached under
    ``{slug}-hero`` so the library/game page picks it up like any other art.
    """
    try:
        result = fetch(source, key=key, ttl=ttl, now_ms=now_ms)
    except Exception as exc:  # noqa: BLE001 - degrade to the muted UI line
        log.debug("news unavailable for %s: %s", key, exc)
        return NewsResult(items=[], available=False)

    base_slug = slug or _slug(key)
    items = [dict(item) for item in result.items]
    hero: str | None = None
    try:
        if hosts:
            for index, item in enumerate(items):
                image = item.get("image")
                if not image:
                    continue
                if index < MAX_NEWS_IMAGES:
                    path = _download_image(
                        image, f"{base_slug}-news-{_item_slug(item['id'])}", "thumb", hosts
                    )
                    if path is not None:
                        item["image_path"] = str(path)
            chosen = _hero_index(items, hero_categories, hero_key)
            if chosen is not None:
                image = items[chosen].get("image")
                hero_path = _download_image(image, base_slug, "official-hero", hosts)
                if hero_path is not None:
                    hero = str(hero_path)
    except Exception as exc:  # noqa: BLE001 - localisation must never raise
        log.debug("news image localisation failed for %s: %s", key, exc)
        hero = None

    return NewsResult(
        items=items,
        partial=result.partial,
        stale=result.stale,
        available=True,
        hero=hero,
    )


__all__ = [
    "CREDIT",
    "CACHE_TTL",
    "MAX_BODY",
    "NewsError",
    "NewsResult",
    "cache_path",
    "fetch",
    "http_get_json",
    "load",
    "normalize_item",
    "read_cache",
    "write_cache",
]
