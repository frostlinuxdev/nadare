# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Yostar official news sources: Arknights Global and Stella Sora.

Both sites run the same CMS; only the list path differs.

The public news list endpoint returns a JSON envelope; only rows from the
publisher's own ``arknights.global`` API are accepted. Images are limited to the
publisher CDN. This module is deliberately free of launcher/GUI concerns: it
returns plain items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ..core.news import NewsError, NewsResult, http_get_json

#: Public news list API (no auth, no browser impersonation).
API = "https://arknights.global/api/resource/news/list"
QUERY = "index=1&size=24&type=latest"

#: Canonical article page, keyed by the numeric news ``id``.
ARTICLE_URL = "https://arknights.global/news/{id}"

#: Only images from the publisher's own CDN are ever downloaded.
IMAGE_HOST = "webusstatic.yo-star.com"
IMAGE_HOSTS: tuple[str, ...] = (IMAGE_HOST,)

#: How many items to keep (newest first).
MAX_ITEMS = 24

#: Hosts whose ``link`` value we trust as an article URL.
LINK_HOSTS = ("arknights.global",)

#: Stella Sora's list endpoint (same envelope/rows; image in ``thumbnail``).
STELLASORA_API = "https://stellasora.global/api/resource/news"
STELLASORA_ARTICLE_URL = "https://stellasora.global/news/{id}"
STELLASORA_LINK_HOSTS = ("stellasora.global",)


def _news_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) > 0:
            return text
    return None


def image_url(raw: Any) -> str | None:
    """Return an allowlisted https image URL, or ``None``.

    Accepts a raw news row (``smallImage`` pref is honoured, falling back to
    ``bigImage``, then Stella Sora's ``thumbnail``) or the URL string itself.
    ``//host/...`` is upgraded to https; any host other than the publisher
    CDN, any userinfo, port, or non-https scheme is rejected.
    """
    candidates: list[Any]
    if isinstance(raw, dict):
        candidates = [raw.get("smallImage"), raw.get("bigImage"), raw.get("thumbnail")]
    else:
        candidates = [raw]
    for value in candidates:
        url = _validated_url(value)
        if url is not None:
            return url
    return None


def _validated_url(value: Any) -> str | None:
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    if parsed.hostname != IMAGE_HOST:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.port is not None:
        return None
    return url


def _article_url(
    raw: dict[str, Any],
    news_id: str,
    link_hosts: tuple[str, ...] = LINK_HOSTS,
    article_url: str = ARTICLE_URL,
) -> str:
    """Prefer an on-site ``link``; otherwise use the canonical article URL."""
    link = str(raw.get("link") or "").strip()
    if link:
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.hostname in link_hosts:
            if not parsed.username and not parsed.password and parsed.port is None:
                return link
    return article_url.format(id=news_id)


def _item(
    raw: Any,
    link_hosts: tuple[str, ...] = LINK_HOSTS,
    article_url: str = ARTICLE_URL,
) -> dict[str, Any] | None:
    """Validate one raw row and map it to the feed shape, or ``None``."""
    if not isinstance(raw, dict):
        return None
    news_id = _news_id(raw.get("id"))
    if news_id is None:
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    try:
        published = int(raw.get("publishTime") or 0)
    except (TypeError, ValueError):
        published = 0
    # The site's own label ("Events") beats the raw type ("activity").
    category = str(raw.get("typeLabel") or "").strip()
    category = category or str(raw.get("type") or "").strip().title()
    return {
        "id": news_id,
        "title": title,
        "summary": "",
        "category": category,
        "published_at": published,
        "image": image_url(raw),
        "url": _article_url(raw, news_id, link_hosts, article_url),
    }


def fetch_arknights_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Arknights Global news; raise on a bad envelope."""
    return _fetch("Arknights", API, LINK_HOSTS, ARTICLE_URL, get_json)


def fetch_stellasora_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Stella Sora news; raise on a bad envelope."""
    return _fetch(
        "Stella Sora", STELLASORA_API, STELLASORA_LINK_HOSTS, STELLASORA_ARTICLE_URL, get_json
    )


def _fetch(
    name: str,
    api: str,
    link_hosts: tuple[str, ...],
    article_url: str,
    get_json: Callable[..., Any] | None,
) -> NewsResult:
    get = get_json or http_get_json
    payload = get(f"{api}?{QUERY}", headers={"Accept": "application/json"})
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise NewsError(f"bad {name} news envelope")
    data = payload.get("data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise NewsError(f"missing {name} news rows")

    collected: list[dict[str, Any]] = []
    for raw in rows:
        item = _item(raw, link_hosts, article_url)
        if item is not None:
            collected.append(item)

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in sorted(collected, key=lambda i: i.get("published_at", 0), reverse=True):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        merged.append(item)
        if len(merged) >= MAX_ITEMS:
            break

    return NewsResult(items=merged)


__all__ = [
    "API",
    "ARTICLE_URL",
    "IMAGE_HOST",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "STELLASORA_API",
    "fetch_arknights_news",
    "fetch_stellasora_news",
    "image_url",
]
