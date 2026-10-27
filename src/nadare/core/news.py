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
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

#: Overall budget for a background news load (feed + images), in seconds.
NEWS_DEADLINE = 15
#: Per-image timeout, so one stuck CDN cannot eat the whole budget.
IMAGE_TIMEOUT = 3


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


def https_on(value: Any, hosts: tuple[str, ...]) -> str | None:
    """``value`` when it is a plain https URL on one of ``hosts``; no userinfo, no port."""
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        return None
    if parsed.username or parsed.password:
        return None
    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    return url


def _parse_json(resp: Any, body: bytes, url: str) -> Any:
    try:
        return json.loads(body) if body else resp.json()
    except (ValueError, TypeError) as exc:
        raise NewsError(f"invalid JSON for {url}") from exc


def _request(
    method: str,
    url: str,
    *,
    parse: Callable[[Any, bytes], Any],
    headers: dict[str, str] | None = None,
    timeout: int = TIMEOUT,
    data: Any = None,
) -> Any:
    """Issue one streamed, non-redirecting request and always release it."""
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged.update(headers)
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "headers": merged,
        "allow_redirects": False,
        "stream": True,
    }
    if data is not None:
        kwargs["data"] = data
    resp = getattr(_curl(), method)(url, **kwargs)
    try:
        status = int(getattr(resp, "status_code", 200) or 200)
        if status != 200:
            raise NewsError(f"HTTP {status} for {url}")
        return parse(resp, _read_capped(resp, MAX_BODY, url))
    finally:
        # ``stream=True`` holds the connection open until the body is consumed
        # (or the response is closed), so always release it rather than waiting
        # for GC on a long-lived GUI process.
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def http_get_json(
    url: str, *, headers: dict[str, str] | None = None, timeout: int = TIMEOUT
) -> Any:
    """GET ``url`` as JSON without browser impersonation or redirects.

    Raises :class:`NewsError` on a non-200 status, an oversized body or invalid
    JSON. The response body is capped at :data:`MAX_BODY` bytes *while reading*,
    so an oversized response never lands in memory whole.
    """
    return _request(
        "get",
        url,
        headers=headers,
        timeout=timeout,
        parse=lambda resp, body: _parse_json(resp, body, url),
    )


def http_get_text(
    url: str, *, headers: dict[str, str] | None = None, timeout: int = TIMEOUT
) -> str:
    """GET ``url`` as UTF-8 text (replacement on invalid bytes), without redirects."""
    return _request(
        "get",
        url,
        headers=headers,
        timeout=timeout,
        parse=lambda _resp, body: body.decode("utf-8", "replace"),
    )


def http_post_json(
    url: str,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = TIMEOUT,
) -> Any:
    """POST ``body`` as JSON and return the JSON response, without redirects."""
    merged = {"Content-Type": "application/json;charset=utf-8"}
    if headers:
        merged.update(headers)
    return _request(
        "post",
        url,
        headers=merged,
        timeout=timeout,
        data=json.dumps(body),
        parse=lambda resp, raw: _parse_json(resp, raw, url),
    )


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


def _download_image(
    url: str,
    slug: str,
    kind: str,
    hosts: tuple[str, ...],
    *,
    timeout: int = IMAGE_TIMEOUT,
    cancel=None,
):
    from . import artwork

    try:
        return artwork.download_compat(
            url, slug, kind, hosts, timeout=timeout, cancel=cancel
        )
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail is not fatal
        log.debug("news image failed (%s): %s", url, exc)
        return None


class _Stopped(Exception):
    """The overall deadline expired or the user cancelled the fetch."""


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel.cancelled)


def _await(call, *, cancel, deadline):
    """Run ``call`` on a daemon thread, returning within ``deadline``.

    The network call itself cannot be interrupted, so a worker that outlives the
    deadline is left detached instead of blocking the GUI (or process shutdown).
    """
    box: queue.Queue = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            box.put((True, call()))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box.put((False, exc))

    threading.Thread(target=run, name="nadare-news-fetch", daemon=True).start()
    while True:
        if _cancelled(cancel):
            raise _Stopped
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Stopped
        try:
            ok, value = box.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if not ok:
            raise value
        return value


def _download_images(tasks, *, hosts, cancel, deadline) -> dict[tuple, str]:
    """Download ``tasks`` concurrently, keeping whatever finishes by ``deadline``."""
    results: dict[tuple, str] = {}
    if not tasks:
        return results
    box: queue.Queue = queue.Queue()

    def run(token, url, slug, kind) -> None:
        if _cancelled(cancel) or time.monotonic() >= deadline:
            box.put((token, None))
            return
        path = _download_image(url, slug, kind, hosts, cancel=cancel)
        box.put((token, path))

    for token, url, slug, kind in tasks:
        threading.Thread(
            target=run,
            args=(token, url, slug, kind),
            name="nadare-news-image",
            daemon=True,
        ).start()

    remaining = len(tasks)
    while remaining:
        if _cancelled(cancel):
            break
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            token, path = box.get(timeout=min(0.05, left))
        except queue.Empty:
            continue
        remaining -= 1
        if path is not None:
            results[token] = str(path)
    return results


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
    deadline: float = NEWS_DEADLINE,
    cancel=None,
) -> NewsResult:
    """Return a UI-ready feed with local thumbnail/hero paths.

    Never raises: an unavailable source yields ``available=False``. The hero is
    the newest item with an image, unless ``hero_categories`` is set, in which
    case the newest item in one of those categories wins (falling back to the
    newest with an image when none match). A ``hero_key`` callable overrides
    both: the item with the greatest non-``None`` key wins. It is cached under
    ``{slug}-hero`` so the library/game page picks it up like any other art.
    """
    end = time.monotonic() + max(0.1, float(deadline))
    try:
        result = _await(
            lambda: fetch(source, key=key, ttl=ttl, now_ms=now_ms),
            cancel=cancel,
            deadline=end,
        )
    except _Stopped:
        log.debug("news load stopped for %s", key)
        return NewsResult(items=[], available=False)
    except Exception as exc:  # noqa: BLE001 - degrade to the muted UI line
        log.debug("news unavailable for %s: %s", key, exc)
        return NewsResult(items=[], available=False)

    base_slug = slug or _slug(key)
    items = [dict(item) for item in result.items]
    hero: str | None = None
    try:
        if hosts:
            tasks: list[tuple[tuple, str, str, str]] = []
            for index, item in enumerate(items):
                image = item.get("image")
                if image and index < MAX_NEWS_IMAGES:
                    slug_ = f"{base_slug}-news-{_item_slug(item['id'])}"
                    tasks.append((("thumb", index), image, slug_, "thumb"))
            chosen = _hero_index(items, hero_categories, hero_key)
            if chosen is not None:
                image = items[chosen].get("image")
                tasks.append((("hero", chosen), image, base_slug, "official-hero"))
            paths = _download_images(
                tasks, hosts=hosts, cancel=cancel, deadline=end
            )
            for index, item in enumerate(items):
                path = paths.get(("thumb", index))
                if path is not None:
                    item["image_path"] = path
            if chosen is not None:
                hero = paths.get(("hero", chosen))
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
    "IMAGE_TIMEOUT",
    "NEWS_DEADLINE",
    "MAX_BODY",
    "NewsError",
    "NewsResult",
    "cache_path",
    "fetch",
    "http_get_json",
    "http_get_text",
    "http_post_json",
    "https_on",
    "load",
    "normalize_item",
    "read_cache",
    "write_cache",
]
