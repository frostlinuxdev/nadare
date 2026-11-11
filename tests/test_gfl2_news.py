# SPDX-License-Identifier: GPL-3.0-or-later
"""#535: Girls' Frontline 2 (Darkwinter global build) news from Sunborn's public web API.

Probed 2026-11-08, anonymous GET, no cookies:
``https://gf2-web-us-api.sunborngame.com/website/news_list/0?page=1&limit=12`` ->
``{code: 0, data: {list: [{Id, Type, Title, Date: "YYYY-MM-DD HH:MM:SS"}]}}`` (Type 1 News,
3 Notices, 4 Events) and ``/website/rotation`` -> ``{code: 0, data: {list: [{PicUrl}]}}``.
Articles open at ``https://gf2exilium.sunborngame.com/NewsInfo?typeId=<Type>&id=<Id>``.
"""

from __future__ import annotations

import pytest

from nadare.backends import gfl2_news
from nadare.core.news import NewsError

PIC = "https://gf2-us-image-cdn.sunborngame.com/prod/website/cms/7D8A.png"


def row(nid=452, kind=3, title="10/28 Server Maintenance Announcement",
        date="2026-10-27 22:00:00"):
    return {"Id": nid, "Type": kind, "Title": title, "Date": date, "Content": "", "Marks": 0}


def routes(rows, pics=(PIC,), rotation_error=False, rotation_exc=None):
    def get(url, **_):
        if url == gfl2_news.API:
            return {"code": 0, "message": "", "data": {"list": rows, "page": 0, "total": len(rows)}}
        if url == gfl2_news.ROTATION:
            if rotation_error:
                raise NewsError("rotation down")
            if rotation_exc is not None:
                raise rotation_exc
            return {"code": 0, "data": {"list": [{"Id": 1, "PicUrl": p} for p in pics]}}
        raise NewsError(f"unexpected {url}")

    return get


def _news(out):
    return [item for item in out.items if item["id"] != "featured"]


def test_items_have_the_news_shape():
    out = gfl2_news.fetch_gfl2_news(get_json=routes([row()]))
    (item,) = _news(out)
    assert item["id"] == "452"
    assert item["title"] == "10/28 Server Maintenance Announcement"
    assert item["category"] == "Notices"
    assert item["url"] == "https://gf2exilium.sunborngame.com/NewsInfo?typeId=3&id=452"
    assert item["published_at"] > 0 and item["image"] is None


def test_newest_first_deduped_and_capped():
    rows = [row(nid=i, date=f"2026-10-{10 + i:02d} 09:00:00") for i in range(1, 16)] + [
        row(nid=3, date="2026-01-01 00:00:00")
    ]
    out = gfl2_news.fetch_gfl2_news(get_json=routes(rows))
    assert len(out.items) == gfl2_news.MAX_ITEMS  # the banner takes the last place
    ids = [item["id"] for item in _news(out)]
    assert len(ids) == len(set(ids)) == gfl2_news.MAX_ITEMS - 1
    assert ids[0] == "15"


def test_the_first_banner_is_a_featured_item_and_other_hosts_are_refused():
    # core.news builds the hero from item images (it drops NewsResult.hero), like Kuro's feed.
    out = gfl2_news.fetch_gfl2_news(get_json=routes([row()]))
    featured = out.items[-1]
    assert featured["id"] == "featured" and featured["image"] == PIC
    assert featured["category"] == "Featured"
    evil = routes([row()], pics=("https://evil.example/x.png",))
    assert [i["id"] for i in gfl2_news.fetch_gfl2_news(get_json=evil).items] == ["452"]


@pytest.mark.parametrize("exc", [NewsError("down"), TimeoutError("slow"), OSError("reset")])
def test_a_failing_banner_request_keeps_the_news(exc):
    out = gfl2_news.fetch_gfl2_news(get_json=routes([row()], rotation_exc=exc))
    assert [i["id"] for i in out.items] == ["452"]


def test_the_banner_reaches_the_page_hero_through_core_news():
    from nadare.core import news

    items = news._normalise(gfl2_news.fetch_gfl2_news(get_json=routes([row()])).items)
    chosen = news._hero_index(items, ())
    assert chosen is not None and items[chosen]["image"] == PIC


@pytest.mark.parametrize("payload", [{"code": 1}, {"code": 0, "data": {}}, [], None])
def test_a_bad_envelope_is_an_error(payload):
    def get(url, **_):
        return payload

    with pytest.raises(NewsError):
        gfl2_news.fetch_gfl2_news(get_json=get)


def test_rows_without_an_id_or_title_are_skipped():
    rows = [row(nid=None), row(title=""), row(nid="../x"), row(nid=7, kind=99)]
    out = gfl2_news.fetch_gfl2_news(get_json=routes(rows))
    assert [item["id"] for item in _news(out)] == ["7"]
    assert out.items[0]["category"] == "News"  # an unknown type reads as plain news


def test_the_darkwinter_build_uses_it_and_the_haoplay_builds_do_not():
    from nadare.core import backends as core_backends

    assert core_backends.get("direct-gfl2").news_source is not None
    assert core_backends.get("direct-gfl2").official_image_hosts == gfl2_news.IMAGE_HOSTS
    # Haoplay's site only has fixed banner images, no feed (checked 2026-11-08).
    assert core_backends.get("direct-gfl2-haoplay").news_source is None
