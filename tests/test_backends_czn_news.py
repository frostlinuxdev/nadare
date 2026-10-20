# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Chaos Zero Nightmare official news source.

The board HTTP call is injected, so nothing here opens a socket.
"""

from __future__ import annotations

import pytest

from nadare.backends.czn import news as czn_news
from nadare.core import news


def _article(**overrides):
    article = {
        "article_id": 1001,
        "channel_seq": 11034,
        "board_seq": 142421,
        "title": "Patch notes",
        "summary": "What changed",
        "create_datetime": 1_700_000_000_000,
        "media_thumbnail_url": "https://d2x8kymwjom7h7.cloudfront.net/a/b.jpg",
        "user_info": {"channel_grade_code": "official"},
    }
    article.update(overrides)
    return article


def _payload(board: int, items: list[dict]):
    return {"code": 0, "value": {"list": items}}


def test_fetch_boards_filters_and_maps_fields() -> None:
    seen: list[str] = []

    def get(url, **kwargs):
        seen.append(url)
        return _payload(
            0,
            [
                _article(),
                # wrong channel
                _article(article_id=2, channel_seq=1),
                # wrong board
                _article(article_id=3, board_seq=999999),
                # not an official author
                _article(article_id=4, user_info={"channel_grade_code": "user"}),
                # bad id
                _article(article_id=0),
            ],
        )

    result = czn_news.fetch_czn_news(get_json=get)

    assert result.partial is False
    assert len(result.items) == 1
    item = result.items[0]
    assert item["id"] == "1001"
    assert item["title"] == "Patch notes"
    assert item["summary"] == "What changed"
    assert item["published_at"] == 1_700_000_000_000
    assert item["image"] == "https://d2x8kymwjom7h7.cloudfront.net/a/b.jpg"
    assert item["url"] == "https://page.onstove.com/chaoszeronightmare/en/view/1001"
    assert len(seen) == len(czn_news.BOARDS)


def test_digit_string_ids_are_accepted() -> None:
    def get(url, **kwargs):
        return _payload(0, [_article(article_id="4242")])

    result = czn_news.fetch_czn_news(get_json=get)
    assert result.items[0]["id"] == "4242"


def test_bad_envelope_fails_that_board(monkeypatch) -> None:
    calls: list[object] = []

    def get(url, **kwargs):
        calls.append(url)
        return {"code": 7, "value": None}

    with pytest.raises(news.NewsError):
        czn_news.fetch_czn_news(get_json=get)
    assert calls  # every board was attempted


def test_partial_when_some_boards_fail() -> None:
    def get(url, **kwargs):
        if "142488" in url:
            raise news.NewsError("boom")
        return _payload(0, [_article()])

    result = czn_news.fetch_czn_news(get_json=get)
    assert result.partial is True
    assert len(result.items) == 1


def test_merges_sorts_dedupes_and_limits() -> None:
    def get(url, **kwargs):
        board = int(url.split("/BOARD/")[1].split("/")[0])
        return _payload(
            board,
            [
                _article(article_id=board * 1000 + i, create_datetime=board * 1000 + i)
                for i in range(10)
            ],
        )

    result = czn_news.fetch_czn_news(get_json=get)
    assert len(result.items) == 24
    timestamps = [i["published_at"] for i in result.items]
    assert timestamps == sorted(timestamps, reverse=True)
    assert len({i["id"] for i in result.items}) == 24


class TestImageAllowlist:
    def _image(self, url):
        return czn_news.image_url({"media_thumbnail_url": url})

    def test_accepts_cloudfront_https(self) -> None:
        assert self._image("https://d2x8kymwjom7h7.cloudfront.net/a.jpg") == (
            "https://d2x8kymwjom7h7.cloudfront.net/a.jpg"
        )

    def test_prefixes_protocol_relative(self) -> None:
        assert self._image("//d2x8kymwjom7h7.cloudfront.net/a.jpg") == (
            "https://d2x8kymwjom7h7.cloudfront.net/a.jpg"
        )

    def test_rejects_other_hosts(self) -> None:
        assert self._image("https://evil.example/a.jpg") is None
        assert self._image("http://d2x8kymwjom7h7.cloudfront.net/a.jpg") is None

    def test_rejects_userinfo_and_port(self) -> None:
        assert self._image("https://u:p@d2x8kymwjom7h7.cloudfront.net/a.jpg") is None
        assert self._image("https://d2x8kymwjom7h7.cloudfront.net:8443/a.jpg") is None

    def test_malformed_port_is_skipped(self) -> None:
        assert self._image("https://d2x8kymwjom7h7.cloudfront.net:abc/a.png") is None
        assert self._image("https://d2x8kymwjom7h7.cloudfront.net:99999/a.png") is None

    def test_empty_is_none(self) -> None:
        assert self._image("") is None
        assert self._image(None) is None


def test_backend_prefers_updates_and_announcements_for_the_hero() -> None:
    from nadare.backends.czn.backend import CznBackend

    assert CznBackend.news_hero_categories == ("Updates", "Announcements")
