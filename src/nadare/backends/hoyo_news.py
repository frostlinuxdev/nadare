# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""HoYoverse official news: Genshin Impact, Honkai: Star Rail, Zenless Zone Zero, Honkai Impact 3rd.

The launcher's public content endpoint returns the posts and banners it shows::

    GET https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameContent
        ?launcher_id=VYTpXlbWo8&game_id=<id>&language=en-us

``posts`` carry a title, a type and a ``MM/DD`` date (no year); ``banners`` carry an image
and the same short link a post may use. A banner whose link matches a post gives that post its
image; the other banners become "Featured" items so the hero image still has a source.

Only the publisher's link hosts and its image CDN are accepted. This module is free of
launcher/GUI concerns: it returns plain items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from ..core.news import NewsError, NewsResult, http_get_json

API = "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameContent"
LAUNCHER_ID = "VYTpXlbWo8"

#: Branch-API game ids of the global releases (the first three are the Sophon ids).
GAME_IDS: dict[str, str] = {
    "genshin": "gopR6Cufr3",
    "hsr": "4ziysqXOQ8",
    "zzz": "U5hbdsT9W7",
    "hi3": "5TIVvvcwtM",
}

#: Hosts whose link we trust as an article URL (the launcher's own short links, HoYoLAB, YouTube).
LINK_HOSTS: tuple[str, ...] = ("hoyo.link", "www.hoyolab.com", "youtu.be")
IMAGE_HOSTS: tuple[str, ...] = ("fastcdn.hoyoverse.com",)

MAX_ITEMS = 12

_CATEGORIES = {
    "POST_TYPE_ACTIVITY": "Events",
    "POST_TYPE_INFO": "Info",
    "POST_TYPE_ANNOUNCE": "Announcements",
}
_DATE_RE = re.compile(r"\A\s*(\d{1,2})/(\d{1,2})\s*\Z")


def _https_on(value: Any, hosts: tuple[str, ...]) -> str | None:
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


def _published_at(value: Any, now: datetime) -> int:
    """``MM/DD`` -> epoch ms (UTC) of its latest occurrence up to tomorrow; 0 if unreadable.

    The launcher gives no year, so the nearest past date is used. A leap day falls back to the
    latest leap year that has it.
    """
    match = _DATE_RE.match(value) if isinstance(value, str) else None
    if not match:
        return 0
    month, day = int(match.group(1)), int(match.group(2))
    limit = (now + timedelta(days=1)).date()
    for year in range(now.year, now.year - 9, -1):
        try:
            candidate = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            continue
        if candidate.date() <= limit:
            return int(candidate.timestamp() * 1000)
    return 0


def _rows(content: dict[str, Any], key: str) -> list[Any]:
    rows = content.get(key)
    if not isinstance(rows, list):
        raise NewsError(f"missing HoYoverse news {key}")
    return rows


def fetch_news(
    game_id: str,
    get_json: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> NewsResult:
    """Return newest-first launcher news for ``game_id``; ``NewsError`` on a bad envelope."""
    get = get_json or http_get_json
    now = now or datetime.now(UTC)
    payload = get(
        f"{API}?launcher_id={LAUNCHER_ID}&game_id={game_id}&language=en-us",
        headers={"Accept": "application/json"},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    content = data.get("content") if isinstance(data, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("retcode") != 0
        or not isinstance(content, dict)
    ):
        raise NewsError("bad HoYoverse news envelope")
    posts = _rows(content, "posts")
    banners = _rows(content, "banners")

    images: dict[str, str] = {}
    featured: list[dict[str, Any]] = []
    for raw in banners:
        image = raw.get("image") if isinstance(raw, dict) else None
        if not isinstance(image, dict):
            continue
        url = _https_on(image.get("url"), IMAGE_HOSTS)
        link = _https_on(image.get("link"), LINK_HOSTS)
        if url and link:
            images.setdefault(link, url)
            featured.append({"id": f"banner-{raw.get('id')}", "link": link, "image": url})

    items: list[dict[str, Any]] = []
    used_links: set[str] = set()
    for raw in posts:
        if not isinstance(raw, dict):
            continue
        post_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        link = _https_on(raw.get("link"), LINK_HOSTS)
        if not post_id or not title or not link:
            continue
        used_links.add(link)
        items.append(
            {
                "id": post_id,
                "title": title,
                "summary": "",
                "category": _CATEGORIES.get(str(raw.get("type") or ""), ""),
                "published_at": _published_at(raw.get("date"), now),
                "image": images.get(link),
                "url": link,
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)
    for banner in featured:
        if banner["link"] not in used_links:
            items.append(
                {
                    "id": banner["id"],
                    "title": "Featured",
                    "summary": "",
                    "category": "Featured",
                    "published_at": 0,
                    "image": banner["image"],
                    "url": banner["link"],
                }
            )

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    return NewsResult(items=unique[:MAX_ITEMS])


def fetch_genshin_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    return fetch_news(GAME_IDS["genshin"], get_json)


def fetch_hsr_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    return fetch_news(GAME_IDS["hsr"], get_json)


def fetch_zzz_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    return fetch_news(GAME_IDS["zzz"], get_json)


def fetch_hi3_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    return fetch_news(GAME_IDS["hi3"], get_json)


__all__ = [
    "API",
    "GAME_IDS",
    "IMAGE_HOSTS",
    "LINK_HOSTS",
    "MAX_ITEMS",
    "fetch_genshin_news",
    "fetch_hi3_news",
    "fetch_hsr_news",
    "fetch_news",
    "fetch_zzz_news",
]
