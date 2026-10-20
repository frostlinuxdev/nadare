# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Chaos Zero Nightmare official news source (STOVE CWMS, public, no auth).

The board list endpoint returns a JSON envelope; only articles authored by the
publisher on CZN's own boards are accepted. Board fetches run in parallel; a
partial failure is tolerated, a total failure raises :class:`NewsError`.

This module is deliberately free of launcher/GUI concerns: it returns plain
items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from ...core.news import NewsError, NewsResult, http_get_json

#: Public STOVE CWMS article list (no auth, no browser impersonation).
API = "https://api.onstove.com/cwms/v3.0/article_group/BOARD/{board}/article/list"
QUERY = "content_yn=N&summary_yn=Y&sort_type_code=LATEST&translation_yn=N&page=1&size=6"

#: The CZN channel and the boards we surface.
CHANNEL_SEQ = 11034
BOARDS: tuple[int, ...] = (142421, 142488, 139238, 139239)
#: Display label per board.
BOARD_NAMES: dict[int, str] = {
    142421: "Updates",
    142488: "Known issues",
    139238: "Announcements",
    139239: "Developer notes",
}

#: Public article page, keyed by ``article_id``.
ARTICLE_URL = "https://page.onstove.com/chaoszeronightmare/en/view/{article_id}"

#: Only images from the publisher's own CDN are ever downloaded.
IMAGE_HOST = "d2x8kymwjom7h7.cloudfront.net"
IMAGE_HOSTS: tuple[str, ...] = (IMAGE_HOST,)

#: How many merged items to keep (newest first).
MAX_ITEMS = 24
#: Parallel board fetches.
_MAX_WORKERS = len(BOARDS)

#: The publisher's grade that marks an article as official.
OFFICIAL_GRADE = "official"


def _board_url(board: int) -> str:
    return f"{API.format(board=board)}?{QUERY}"


def _article_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) > 0:
            return text
    return None


def _board_seq(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def image_url(raw: Any) -> str | None:
    """Return an allowlisted https image URL, or ``None``.

    Accepts a raw article dict (``media_thumbnail_url``) or the URL string
    itself. ``//host/...`` is upgraded to https; any host other than the
    publisher CDN, any userinfo, port, or non-https scheme is rejected.
    """
    value = raw.get("media_thumbnail_url") if isinstance(raw, dict) else raw
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
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return None
    return url


def _article(raw: Any) -> dict[str, Any] | None:
    """Validate one raw article and map it to the feed shape, or ``None``."""
    if not isinstance(raw, dict):
        return None
    if str(raw.get("channel_seq")) != str(CHANNEL_SEQ):
        return None
    if _board_seq(raw.get("board_seq")) not in BOARDS:
        return None
    board = _board_seq(raw.get("board_seq"))
    user_info = raw.get("user_info")
    grade = (user_info or {}).get("channel_grade_code") if isinstance(user_info, dict) else None
    if grade != OFFICIAL_GRADE:
        return None
    article_id = _article_id(raw.get("article_id"))
    if article_id is None:
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    try:
        published = int(raw.get("create_datetime") or 0)
    except (TypeError, ValueError):
        published = 0
    return {
        "id": article_id,
        "title": title,
        "summary": str(raw.get("summary") or ""),
        "category": BOARD_NAMES.get(board, ""),
        "published_at": published,
        "image": image_url(raw),
        "url": ARTICLE_URL.format(article_id=article_id),
    }


def _fetch_board(board: int, get_json: Callable[..., Any]) -> list[dict[str, Any]]:
    payload = get_json(_board_url(board), headers={"Accept": "application/json"})
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise NewsError(f"bad news envelope for board {board}")
    value = payload.get("value")
    raw_items = value.get("list") if isinstance(value, dict) else None
    if not isinstance(raw_items, list):
        raise NewsError(f"missing article list for board {board}")
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        item = _article(raw)
        if item is not None:
            items.append(item)
    return items


def fetch_czn_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return merged, newest-first CZN news; raise only if every board fails."""
    get = get_json or http_get_json
    collected: list[dict[str, Any]] = []
    failures = 0

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_board, board, get) for board in BOARDS]
        for future in futures:
            try:
                collected.extend(future.result())
            except Exception:  # noqa: BLE001 - a single board must not kill the feed
                failures += 1

    if failures == len(BOARDS):
        raise NewsError("all CZN news boards failed")

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in sorted(collected, key=lambda i: i.get("published_at", 0), reverse=True):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        merged.append(item)
        if len(merged) >= MAX_ITEMS:
            break

    return NewsResult(items=merged, partial=failures > 0)


__all__ = [
    "API",
    "BOARDS",
    "ARTICLE_URL",
    "IMAGE_HOST",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "fetch_czn_news",
    "image_url",
]
