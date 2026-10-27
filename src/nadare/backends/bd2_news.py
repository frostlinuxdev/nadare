# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Brown Dust 2 official notices from the public web API.

``GET /api/notices?locale=en-us`` returns ``{items: [{id, subject, category,
publishedAt, contentPreview}], ...}`` with ISO 8601 UTC timestamps. This module
is free of launcher/GUI concerns: it returns plain items in the
:mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_json

API = "https://webapi.browndust2.com/api/notices?locale=en-us&page=0&limit=12"
ARTICLE = "https://www.browndust2.com/en-us/news/view?id={id}"
IMAGE_HOSTS: tuple[str, ...] = ()
MAX_ITEMS = 12

_ID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_CATEGORIES = {
    "notice": "Notice",
    "update": "Update",
    "event": "Event",
    "patch": "Patch",
}


def _news_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.match(text) else None


def _published_at(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _category(value: Any) -> str:
    text = str(value or "").strip()
    return _CATEGORIES.get(text.lower(), text.replace("_", " ").title())


def fetch_bd2_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Brown Dust 2 notices; ``NewsError`` on a bad envelope."""
    get = get_json or http_get_json
    payload = get(API, headers={"Accept": "application/json"})
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise NewsError("bad Brown Dust 2 news envelope")

    items: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        news_id = _news_id(raw.get("id"))
        title = str(raw.get("subject") or "").strip()
        if not news_id or not title:
            continue
        items.append(
            {
                "id": news_id,
                "title": title,
                "summary": str(raw.get("contentPreview") or "").strip(),
                "category": _category(raw.get("category")),
                "published_at": _published_at(raw.get("publishedAt")),
                "image": None,
                "url": ARTICLE.format(id=news_id),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    return NewsResult(items=unique[:MAX_ITEMS])


__all__ = [
    "API",
    "ARTICLE",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "fetch_bd2_news",
]
