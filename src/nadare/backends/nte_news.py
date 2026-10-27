# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Neverness to Everness official news from the site's static JavaScript feed.

The main page names a dated ``/include/newsData<YYYYMMDD>.js`` script. That file
is not quite JSON: it assigns ``var newsdataObj = {...};`` and carries trailing
commas. The ``en`` object holds several headline columns; they are merged and
deduplicated by article URL. Only ``/en/`` links on the publisher's own site are
accepted and the list carries no images. This module is free of launcher/GUI
concerns: it returns plain items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_text

PAGE = "https://nte.perfectworld.com/en/main.html"
HOST = "https://nte.perfectworld.com"
IMAGE_HOSTS: tuple[str, ...] = ()
MAX_ITEMS = 12

_SCRIPT_RE = re.compile(r"/include/newsData\d+\.js")
_ASSIGN_RE = re.compile(r"\A\s*var\s+newsdataObj\s*=\s*")
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_ARTICLE_ID_RE = re.compile(r"/(\d+)\.html")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _script_url(html: str) -> str:
    match = _SCRIPT_RE.search(html)
    if not match:
        raise NewsError("no NTE news script on the main page")
    return HOST + match.group(0)


def _payload(text: str) -> dict[str, Any]:
    match = _ASSIGN_RE.match(text)
    if not match:
        raise NewsError("bad NTE news script")
    body = text[match.end() :].strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    body = _TRAILING_COMMA_RE.sub(r"\1", body)
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise NewsError("unparseable NTE news script") from exc
    if not isinstance(data, dict):
        raise NewsError("bad NTE news envelope")
    return data


def _published_at(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return 0
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1000)


def _rows(english: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for column in english.values():
        if isinstance(column, list):
            out.extend(raw for raw in column if isinstance(raw, dict))
    return out


def fetch_nte_news(get_text: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first NTE news; ``NewsError`` on any parse failure."""
    get = get_text or http_get_text
    script = _script_url(get(PAGE))
    english = _payload(get(script)).get("en")
    if not isinstance(english, dict):
        raise NewsError("bad NTE news envelope")

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in _rows(english):
        title = str(raw.get("title") or "").strip()
        path = str(raw.get("url") or "").strip()
        if not title or not path.startswith("/en/"):
            continue
        article_id = _ARTICLE_ID_RE.search(path)
        if not article_id or not _ID_RE.match(article_id.group(1)):
            continue
        url = HOST + path
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            {
                "id": article_id.group(1),
                "title": title,
                "summary": "",
                "category": str(raw.get("channelDescription") or "").strip(),
                "published_at": _published_at(raw.get("time")),
                "image": None,
                "url": url,
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return NewsResult(items=items[:MAX_ITEMS])


__all__ = [
    "HOST",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "PAGE",
    "fetch_nte_news",
]
