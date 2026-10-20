# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Arknights Global official news source.

The list HTTP call is injected, so nothing here opens a socket.
"""

from __future__ import annotations

import pytest

from nadare.backends import arknights_news
from nadare.core import news


def _row(**overrides):
    row = {
        "id": 1001,
        "title": "A new event",
        "publishTime": 1_700_000_000_000,
        "type": "event",
        "smallImage": "https://webusstatic.yo-star.com/web-cms/a/small.png",
        "bigImage": "https://webusstatic.yo-star.com/web-cms/a/big.png",
        "link": "https://arknights.global/news/1001",
    }
    row.update(overrides)
    return row


def _payload(rows, code: int = 0):
    return {"code": code, "message": "ok", "data": {"count": len(rows), "rows": rows}}


def test_fetch_maps_fields_and_uses_canonical_url() -> None:
    seen: list[str] = []

    def get(url, **kwargs):
        seen.append(url)
        return _payload([_row()])

    result = arknights_news.fetch_arknights_news(get_json=get)

    assert result.partial is False
    assert len(result.items) == 1
    item = result.items[0]
    assert item["id"] == "1001"
    assert item["title"] == "A new event"
    assert item["summary"] == ""
    assert item["category"] == "Event"
    assert item["published_at"] == 1_700_000_000_000
    assert item["image"] == "https://webusstatic.yo-star.com/web-cms/a/small.png"
    assert item["url"] == "https://arknights.global/news/1001"
    assert seen and seen[0].startswith(arknights_news.API)


def test_type_is_title_cased_category() -> None:
    def get(url, **kwargs):
        return _payload([_row(type="known issues")])

    result = arknights_news.fetch_arknights_news(get_json=get)
    assert result.items[0]["category"] == "Known Issues"


def test_ignores_offsite_link() -> None:
    def get(url, **kwargs):
        return _payload([_row(link="https://evil.example/news/1001")])

    result = arknights_news.fetch_arknights_news(get_json=get)
    assert result.items[0]["url"] == "https://arknights.global/news/1001"


def test_bad_envelope_raises() -> None:
    def get(url, **kwargs):
        return {"code": 7, "message": "nope"}

    with pytest.raises(news.NewsError):
        arknights_news.fetch_arknights_news(get_json=get)

    def get_no_rows(url, **kwargs):
        return {"code": 0, "data": {"count": 0}}

    with pytest.raises(news.NewsError):
        arknights_news.fetch_arknights_news(get_json=get_no_rows)


def test_sorts_and_dedupes() -> None:
    def get(url, **kwargs):
        return _payload(
            [
                _row(id=1, publishTime=100),
                _row(id=2, publishTime=300),
                _row(id=1, publishTime=100),
                _row(id=3, publishTime=200),
            ]
        )

    result = arknights_news.fetch_arknights_news(get_json=get)
    assert [i["id"] for i in result.items] == ["2", "3", "1"]


def test_bad_rows_are_dropped() -> None:
    def get(url, **kwargs):
        return _payload(
            [
                _row(),
                {"id": 0, "title": "zero", "publishTime": 1},
                {"id": 5, "title": "", "publishTime": 1},
                "not a dict",
            ]
        )

    result = arknights_news.fetch_arknights_news(get_json=get)
    assert [i["id"] for i in result.items] == ["1001"]


def test_image_falls_back_to_big_image() -> None:
    def get(url, **kwargs):
        return _payload([_row(smallImage="", bigImage="https://webusstatic.yo-star.com/big.png")])

    result = arknights_news.fetch_arknights_news(get_json=get)
    assert result.items[0]["image"] == "https://webusstatic.yo-star.com/big.png"


class TestImageAllowlist:
    def test_accepts_yostar_https(self) -> None:
        url = "https://webusstatic.yo-star.com/web-cms/a/small.png"
        assert arknights_news.image_url({"smallImage": url}) == url

    def test_rejects_other_hosts(self) -> None:
        assert arknights_news.image_url({"smallImage": "https://evil.example/a.png"}) is None
        bad_scheme = {"smallImage": "http://webusstatic.yo-star.com/a.png"}
        assert arknights_news.image_url(bad_scheme) is None

    def test_rejects_userinfo_and_port(self) -> None:
        assert (
            arknights_news.image_url({"smallImage": "https://u:p@webusstatic.yo-star.com/a.png"})
            is None
        )
        assert (
            arknights_news.image_url({"smallImage": "https://webusstatic.yo-star.com:8443/a.png"})
            is None
        )

    def test_empty_is_none(self) -> None:
        assert arknights_news.image_url({"smallImage": ""}) is None
        assert arknights_news.image_url("") is None
        assert arknights_news.image_url(None) is None


def test_backend_wires_the_source_and_hosts() -> None:
    from nadare.backends.arknights import ArknightsBackend

    assert ArknightsBackend.news_source is arknights_news.fetch_arknights_news
    assert ArknightsBackend.official_image_hosts == arknights_news.IMAGE_HOSTS
    assert ArknightsBackend.news_hero_categories == ()


def _ss_row(**overrides):
    row = {
        "id": 4770,
        "title": "New event",
        "publishTime": 1_790_000_000_000,
        "type": "activity",
        "thumbnail": "https://webusstatic.yo-star.com/web-cms-prod/upload/x.png",
        "link": "https://stellasora.global/news/4770",
    }
    row.update(overrides)
    return row


def test_stellasora_uses_its_endpoint_thumbnail_and_link() -> None:
    seen: list[str] = []

    def get(url, **kwargs):
        seen.append(url)
        return _payload([_ss_row()])

    item = arknights_news.fetch_stellasora_news(get_json=get).items[0]
    assert seen[0].startswith(arknights_news.STELLASORA_API + "?")
    assert item["image"] == "https://webusstatic.yo-star.com/web-cms-prod/upload/x.png"
    assert item["url"] == "https://stellasora.global/news/4770"


def test_stellasora_rejects_foreign_link_and_image_hosts() -> None:
    def get(url, **kwargs):
        return _payload(
            [_ss_row(link="https://arknights.global/news/1", thumbnail="https://evil.example/x.png")]
        )

    item = arknights_news.fetch_stellasora_news(get_json=get).items[0]
    assert item["url"] == "https://stellasora.global/news/4770"
    assert item["image"] is None


def test_stellasora_backend_has_official_news() -> None:
    from nadare.backends.stellasora import StellaSoraBackend

    assert StellaSoraBackend.news_source is not None
    assert "webusstatic.yo-star.com" in StellaSoraBackend.official_image_hosts


def test_site_type_label_wins_over_raw_type() -> None:
    def get(url, **kwargs):
        return _payload([_ss_row(type="activity", typeLabel="Events")])

    assert arknights_news.fetch_stellasora_news(get_json=get).items[0]["category"] == "Events"
