# SPDX-License-Identifier: GPL-3.0-or-later
"""HoYoverse launcher news (hyp-connect getGameContent): parsing, allowlists, dates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nadare.backends import hoyo_news
from nadare.core.news import NewsError

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
IMG = "https://fastcdn.hoyoverse.com/static-resource-v2/2026/10/26/a.jpg"


def payload(posts=(), banners=()):
    return {
        "retcode": 0,
        "message": "OK",
        "data": {"content": {"posts": list(posts), "banners": list(banners)}},
    }


def post(
    pid="p1",
    title="Version 4.6 Update",
    link="https://hoyo.link/abc",
    date="10/20",
    kind="POST_TYPE_ANNOUNCE",
):
    return {"id": pid, "type": kind, "title": title, "link": link, "date": date}


def banner(bid="b1", url=IMG, link="https://hoyo.link/abc"):
    return {"id": bid, "image": {"url": url, "link": link}}


def run(data, **kw):
    return hoyo_news.fetch_news("gameid", get_json=lambda url, **_: data, now=NOW, **kw)


def test_the_request_names_the_launcher_and_the_game():
    seen = []

    def get(url, **_):
        seen.append(url)
        return payload([post()])

    hoyo_news.fetch_news("4ziysqXOQ8", get_json=get, now=NOW)
    assert seen == [
        "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameContent"
        "?launcher_id=VYTpXlbWo8&game_id=4ziysqXOQ8&language=en-us"
    ]


def test_a_post_becomes_an_item_with_its_category_and_year_guessed_from_today():
    item = run(payload([post(date="10/20", kind="POST_TYPE_ACTIVITY")])).items[0]
    assert item["id"] == "p1" and item["title"] == "Version 4.6 Update"
    assert item["url"] == "https://hoyo.link/abc" and item["category"] == "Events"
    assert item["published_at"] == int(datetime(2026, 10, 20, tzinfo=UTC).timestamp() * 1000)
    assert item["image"] is None


def test_a_date_later_than_today_belongs_to_last_year():
    item = run(payload([post(date="12/30")])).items[0]
    assert item["published_at"] == int(datetime(2025, 12, 30, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize("date", ["", "13/40", "abc", "2/30", None, 5])
def test_an_unreadable_date_keeps_the_post_with_no_date(date):
    items = run(payload([post(date=date)])).items
    assert len(items) == 1 and items[0]["published_at"] == 0


def test_a_leap_day_is_read_in_the_nearest_leap_year():
    item = run(payload([post(date="02/29")])).items[0]
    assert item["published_at"] == int(datetime(2024, 2, 29, tzinfo=UTC).timestamp() * 1000)


def test_a_banner_with_the_same_link_gives_the_post_its_image():
    items = run(payload([post()], [banner()])).items
    assert len(items) == 1 and items[0]["image"] == IMG


def test_a_banner_without_a_matching_post_is_a_featured_item():
    items = run(payload([post()], [banner(link="https://hoyo.link/other")])).items
    assert [i["title"] for i in items] == ["Version 4.6 Update", "Featured"]
    assert items[1]["image"] == IMG and items[1]["url"] == "https://hoyo.link/other"
    assert items[0]["image"] is None


def test_newest_first_with_undated_banners_last():
    data = payload(
        [
            post("a", date="10/01", link="https://hoyo.link/a"),
            post("b", date="10/22", link="https://hoyo.link/b"),
        ],
        [banner("x", link="https://hoyo.link/zzz")],
    )
    assert [i["id"] for i in run(data).items] == ["b", "a", "banner-x"]


@pytest.mark.parametrize(
    "link",
    [
        "http://hoyo.link/abc",
        "https://evil.example/abc",
        "https://hoyo.link@evil.example/abc",
        "https://user:pw@hoyo.link/abc",
        "https://hoyo.link:8443/abc",
        "javascript:alert(1)",
        "",
        None,
    ],
)
def test_a_post_with_an_untrusted_link_is_dropped(link):
    assert run(payload([post(link=link)])).items == []


@pytest.mark.parametrize("host", ["hoyo.link", "www.hoyolab.com", "youtu.be"])
def test_the_publisher_link_hosts_are_accepted(host):
    assert len(run(payload([post(link=f"https://{host}/x")])).items) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://fastcdn.hoyoverse.com/a.jpg",
        "https://evil.example/a.jpg",
        "https://fastcdn.hoyoverse.com@evil.example/a.jpg",
        "https://fastcdn.hoyoverse.com:444/a.jpg",
        "",
        None,
    ],
)
def test_a_banner_image_off_the_publisher_cdn_is_ignored(url):
    items = run(payload([post()], [banner(url=url)])).items
    assert [i["image"] for i in items] == [None]


def test_a_post_without_id_or_title_is_dropped():
    assert run(payload([post(pid=""), post(title=" "), {"id": 5}, "junk", None])).items == []


def test_duplicate_ids_collapse_and_the_list_is_capped():
    many = [post(f"p{n}", link=f"https://hoyo.link/{n}") for n in range(40)]
    result = run(payload(many + [post("p0")]))
    ids = [i["id"] for i in result.items]
    assert len(ids) == len(set(ids)) == hoyo_news.MAX_ITEMS


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"retcode": 1, "message": "x"},
        {"retcode": 0, "data": None},
        {"retcode": 0, "data": {"content": None}},
        {"retcode": 0, "data": {"content": {"posts": "x", "banners": []}}},
        {"retcode": 0, "data": {"content": {"posts": [], "banners": 3}}},
    ],
)
def test_a_bad_envelope_raises_news_error(data):
    with pytest.raises(NewsError):
        run(data)


def test_every_hoyo_backend_has_a_news_source_and_the_cdn_host():
    from nadare.backends import direct

    for cls in (
        direct.GenshinBackend,
        direct.StarRailBackend,
        direct.ZenlessBackend,
        direct.Hi3Backend,
    ):
        assert cls.news_source is not None, cls
        assert cls.official_image_hosts == ("fastcdn.hoyoverse.com",)
    assert direct.PgrBackend.news_source is not hoyo_news.fetch_hi3_news


def test_the_game_ids_match_the_ones_the_installer_uses():
    from nadare.backends import direct
    from nadare.core.sources import hoyo

    ids = hoyo._SOPHON_GAME_IDS
    assert hoyo_news.GAME_IDS == {
        "genshin": ids["hk4e_global"],
        "hsr": ids["hkrpg_global"],
        "zzz": ids["nap_global"],
        "hi3": "5TIVvvcwtM",
    }
    assert direct.GenshinBackend.news_source.__name__ == "fetch_genshin_news"
