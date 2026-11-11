# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Girls' Frontline 2: Exilium (Darkwinter global build) news from Sunborn's public web API.

``GET /website/news_list/0?page=1&limit=N`` returns ``{code: 0, data: {list: [{Id, Type,
Title, Date}]}}`` with ``Date`` as ``YYYY-MM-DD HH:MM:SS``; ``Type`` is 1 News, 3 Notices,
4 Events (the site's own table). ``/website/rotation`` lists the site's banners; the first one
on the publisher's image CDN is added as a trailing "Featured" item (``core.news`` takes the page
hero from item images, as for Kuro's feed). A failing banner request never hides the news.
This module is free of launcher/GUI concerns: it returns plain items in the
:mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_json, https_on

MAX_ITEMS = 12
_BASE = "https://gf2-web-us-api.sunborngame.com/website"
API = f"{_BASE}/news_list/0?page=1&limit={MAX_ITEMS}"
ROTATION = f"{_BASE}/rotation"
ARTICLE = "https://gf2exilium.sunborngame.com/NewsInfo?typeId={kind}&id={id}"
IMAGE_HOSTS: tuple[str, ...] = ("gf2-us-image-cdn.sunborngame.com",)

_CATEGORIES = {1: "News", 3: "Notices", 4: "Events"}
_ID_RE = re.compile(r"\A[0-9]{1,12}\Z")


def _news_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.match(text) else None


def _kind(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _published_at(value: Any) -> int:
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1000)


def _rows(payload: Any) -> list[Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(rows, list):
        raise NewsError("bad Girls' Frontline 2 news envelope")
    return rows


def _banner(get: Callable[..., Any]) -> str | None:
    try:
        for raw in _rows(get(ROTATION, headers={"Accept": "application/json"})):
            if isinstance(raw, dict):
                url = https_on(raw.get("PicUrl"), IMAGE_HOSTS)
                if url:
                    return url
    except Exception:  # noqa: BLE001 - a timeout or reset on the banner must not hide the news
        pass
    return None


def fetch_gfl2_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first GFL2 news; ``NewsError`` on a bad envelope."""
    get = get_json or http_get_json
    rows = _rows(get(API, headers={"Accept": "application/json"}))

    items: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        news_id = _news_id(raw.get("Id"))
        title = str(raw.get("Title") or "").strip()
        if not news_id or not title:
            continue
        kind = _kind(raw.get("Type"))
        items.append(
            {
                "id": news_id,
                "title": title,
                "summary": "",
                "category": _CATEGORIES.get(kind, "News"),
                "published_at": _published_at(raw.get("Date")),
                "image": None,
                "url": ARTICLE.format(kind=kind, id=news_id),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    banner = _banner(get)
    unique = unique[: MAX_ITEMS - 1 if banner else MAX_ITEMS]
    if banner:
        unique.append(
            {
                "id": "featured",
                "title": "Featured",
                "summary": "",
                "category": "Featured",
                "published_at": 0,
                "image": banner,
                "url": "https://gf2exilium.sunborngame.com/",
            }
        )
    return NewsResult(items=unique)


__all__ = ["API", "ARTICLE", "IMAGE_HOSTS", "MAX_ITEMS", "ROTATION", "fetch_gfl2_news"]
