# SPDX-License-Identifier: GPL-3.0-or-later
"""Publisher news feeds: Kuro (WuWa, PGR), NIKKE, NTE, BD2, Endfield."""

from __future__ import annotations

import pytest

from nadare.backends import bd2_news, endfield_news, kuro_news, nikke_news, nte_news
from nadare.core.news import NewsError

HOUR = 3600 * 1000


def by_url(routes):
    def get(url, **_):
        if url not in routes:
            raise NewsError(f"unexpected {url}")
        value = routes[url]
        if isinstance(value, Exception):
            raise value
        return value

    return get


# --------------------------------------------------------------------------- Kuro
WUWA_LIST = kuro_news.WUWA_LIST
WUWA_MAIN = kuro_news.WUWA_MAIN
COVER = "https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/images/1/a.png"


def article(aid, title="Convene", kind=58, start="2026-10-20 18:00:00"):
    return {
        "articleId": aid,
        "articleTitle": title,
        "articleType": kind,
        "startTime": start,
        "top": 0,
    }


def main(cover=COVER):
    return {
        "articleType": [
            {"contentId": 57, "contentLabel": "News"},
            {"contentId": 58, "contentLabel": "Notice"},
        ],
        "pcTopPicture": {"coverImage": cover},
    }


def kuro(items, main_payload=None, game="wuwa"):
    routes = {WUWA_LIST: items, WUWA_MAIN: main_payload if main_payload is not None else main()}
    return kuro_news.fetch_wuwa_news(get_json=by_url(routes))


def test_kuro_items_are_sorted_by_start_time_not_list_order():
    out = kuro([article(1, start="2026-01-01 00:00:00"), article(2, start="2026-10-20 18:00:00")])
    assert [i["id"] for i in out.items if i["category"] != "Featured"] == ["2", "1"]


def test_kuro_item_fields_label_and_article_url():
    item = kuro([article(5668, "Phase II", 57, "2026-10-20 18:00:00")]).items[0]
    assert item["title"] == "Phase II" and item["category"] == "News"
    assert item["url"] == "https://wutheringwaves.kurogames.com/en/main/news/detail/5668"
    assert item["published_at"] == 1792519200000  # 2026-10-20 18:00:00 UTC


def test_kuro_cover_becomes_a_featured_item_so_the_hero_has_art():
    items = kuro([article(1)]).items
    featured = items[-1]
    assert featured["category"] == "Featured" and featured["image"] == COVER
    assert featured["url"].startswith("https://wutheringwaves.kurogames.com/")


def test_kuro_cover_on_an_untrusted_host_is_dropped():
    items = kuro([article(1)], main("https://evil.example/a.png")).items
    assert all(i["category"] != "Featured" for i in items)


def test_kuro_a_dead_main_menu_keeps_the_articles():
    routes = {WUWA_LIST: [article(1)], WUWA_MAIN: NewsError("503")}
    out = kuro_news.fetch_wuwa_news(get_json=by_url(routes))
    assert [i["id"] for i in out.items] == ["1"]


def test_kuro_a_dead_article_list_is_an_error():
    routes = {WUWA_LIST: NewsError("503"), WUWA_MAIN: main()}
    with pytest.raises(NewsError):
        kuro_news.fetch_wuwa_news(get_json=by_url(routes))


def test_kuro_bad_rows_are_skipped_and_the_feed_is_capped():
    rows = [{"articleId": None}, "x", article(0, "")] + [
        article(i + 10, start=f"2026-01-{i % 28 + 1:02d} 00:00:00") for i in range(40)
    ]
    out = kuro(rows)
    assert len(out.items) <= 12 and all(i["title"] for i in out.items)


def test_kuro_the_cap_never_drops_the_featured_cover():
    rows = [article(i + 10, start=f"2026-01-{i % 28 + 1:02d} 00:00:00") for i in range(40)]
    items = kuro(rows).items
    assert len(items) == 12 and items[-1]["category"] == "Featured"


def test_kuro_pgr_uses_its_own_cms_and_article_pages():
    routes = {
        kuro_news.PGR_LIST: [article(4535, "Top-Up", 47)],
        kuro_news.PGR_MAIN: {
            "articleType": [{"contentId": 47, "contentLabel": "News"}],
            "pcTopPicture": {
                "coverImage": "https://media-cdn-zspms.kurogame.net/pnswebsite/website2.0/images/1/b.png"
            },
        },
    }
    items = kuro_news.fetch_pgr_news(get_json=by_url(routes)).items
    assert (
        items[0]["url"] == "https://pgr.kurogame.net/news/4535" and items[0]["category"] == "News"
    )
    assert items[-1]["category"] == "Featured"


def test_kuro_image_hosts_cover_both_cdns():
    assert "hw-media-cdn-mingchao.kurogame.com" in kuro_news.IMAGE_HOSTS
    assert "media-cdn-zspms.kurogame.net" in kuro_news.IMAGE_HOSTS


# --------------------------------------------------------------------------- NIKKE
PIC = "https://na-nikke-aws.playerinfinite.com/cms/nrft/feeds/pic/a.jpeg"


def nikke_payload(rows):
    return {"code": 0, "msg": "succ", "data": {"info_content": rows}}


def nikke_row(cid="9ff3fe24ab", title="Update on October 8", ts="1791187800", pics=(PIC,)):
    return {
        "content_id": cid,
        "title": title,
        "pub_timestamp": ts,
        "pic_urls": list(pics),
        "content_part": "Commanders,\nbody",
    }


def run_nikke(payload):
    seen = []

    def post(url, body, **kw):
        seen.append((url, body, kw))
        return payload

    return nikke_news.fetch_nikke_news(post_json=post), seen


def test_nikke_posts_the_news_label_with_the_site_headers():
    _, seen = run_nikke(nikke_payload([nikke_row()]))
    url, body, kw = seen[0]
    assert url == (
        "https://na-community.playerinfinite.com/api/"
        "gpts.information_feeds_svr.InformationFeedsSvr/GetContentByLabel"
    )
    assert body["primary_label_id"] == 309 and body["secondary_label_id"] == 496
    assert kw["headers"]["X-GameId"] == "16" and kw["headers"]["X-AreaId"] == "na"


def test_nikke_item_fields():
    item = run_nikke(nikke_payload([nikke_row()]))[0].items[0]
    assert item["id"] == "9ff3fe24ab" and item["title"] == "Update on October 8"
    assert item["published_at"] == 1791187800000 and item["image"] == PIC
    assert item["url"] == "https://nikke-en.com/newsdetail.html?content_id=9ff3fe24ab"


def test_nikke_untrusted_image_hosts_and_hostile_ids_are_dropped():
    out, _ = run_nikke(
        nikke_payload(
            [nikke_row(pics=("https://evil.example/a.jpg",)), nikke_row(cid="../x", title="bad")]
        )
    )
    assert len(out.items) == 1 and out.items[0]["image"] is None


def test_nikke_bad_envelope_raises():
    for bad in ({"code": 5}, {"code": 0, "data": {}}, [], None):
        with pytest.raises(NewsError):
            run_nikke(bad)


# --------------------------------------------------------------------------- NTE
NTE_HTML = '<html><script src="/include/newsData20260112.js"></script></html>'
NTE_JS = """var newsdataObj = {
    "cn" : { "news": [ {"title":"x","url":"/cn/a/1.html","time":"2026-10-19","channelDescription":"n","channelName":"gamenews"} ] },
    "en" : {
        "news": [
            {"title":"Server Maintenance Notice: October 21","url":"/en/article/news/gamenews/20261019/264675.html","time":"2026-10-19","channelDescription":"News","channelName":"gamenews"}
            ,
            {"title":"Account Action Notice","url":"/en/article/news/gamebroad/20261012/264504.html","time":"2026-10-12","channelDescription":"Notices","channelName":"gamebroad"}
            ,
        ],
        "gamebroad": [
            {"title":"Account Action Notice","url":"/en/article/news/gamebroad/20261012/264504.html","time":"2026-10-12","channelDescription":"Notices","channelName":"gamebroad"},
        ],
        "gameevent": [
        ],
    }
};
"""  # noqa: E501


def run_nte(html=NTE_HTML, js=NTE_JS):
    seen = []

    def get(url, **_):
        seen.append(url)
        if url == "https://nte.perfectworld.com/en/main.html":
            return html
        if url == "https://nte.perfectworld.com/include/newsData20260112.js":
            return js
        raise NewsError(url)

    return nte_news.fetch_nte_news(get_text=get), seen


def test_nte_reads_the_script_name_from_the_main_page():
    _, seen = run_nte()
    assert seen == [
        "https://nte.perfectworld.com/en/main.html",
        "https://nte.perfectworld.com/include/newsData20260112.js",
    ]


def test_nte_parses_the_english_columns_despite_trailing_commas_and_dedupes():
    out, _ = run_nte()
    assert [i["id"] for i in out.items] == ["264675", "264504"]
    first = out.items[0]
    assert first["title"] == "Server Maintenance Notice: October 21" and first["category"] == "News"
    assert (
        first["url"] == "https://nte.perfectworld.com/en/article/news/gamenews/20261019/264675.html"
    )
    assert first["published_at"] == 1792368000000  # 2026-10-19 UTC
    assert first["image"] is None


def test_nte_rejects_a_missing_script_or_unparseable_file():
    for html, js in (("<html></html>", NTE_JS), (NTE_HTML, "var newsdataObj = {oops")):
        with pytest.raises(NewsError):
            run_nte(html, js)


def test_nte_drops_links_that_leave_the_site():
    js = NTE_JS.replace("/en/article/news/gamenews/20261019/264675.html", "//evil.example/x.html")
    out, _ = run_nte(js=js)
    assert [i["id"] for i in out.items] == ["264504"]


# --------------------------------------------------------------------------- BD2
def bd2_payload(rows):
    return {"items": rows, "totalCount": len(rows), "page": 0, "totalPages": 1}


def bd2_row(
    rid="01M5VNAY", subject="BrownDust2 Night Live", cat="notice", at="2026-10-26T17:00:44.954Z"
):
    return {
        "id": rid,
        "subject": subject,
        "category": cat,
        "publishedAt": at,
        "contentPreview": "Don't miss it",
    }


def test_bd2_item_fields():
    out = bd2_news.fetch_bd2_news(get_json=lambda url, **_: bd2_payload([bd2_row()]))
    item = out.items[0]
    assert item["id"] == "01M5VNAY" and item["title"] == "BrownDust2 Night Live"
    assert item["category"] == "Notice" and item["summary"] == "Don't miss it"
    assert item["published_at"] == 1793034044954
    assert item["url"] == "https://www.browndust2.com/en-us/news/view?id=01M5VNAY"


def test_bd2_asks_for_english_and_sorts_newest_first():
    seen = []

    def get(url, **_):
        seen.append(url)
        return bd2_payload(
            [bd2_row("a", at="2026-01-01T00:00:00Z"), bd2_row("b", at="2026-02-01T00:00:00Z")]
        )

    out = bd2_news.fetch_bd2_news(get_json=get)
    assert "locale=en-us" in seen[0] and seen[0].startswith(
        "https://webapi.browndust2.com/api/notices"
    )
    assert [i["id"] for i in out.items] == ["b", "a"]


def test_bd2_bad_envelope_and_hostile_ids():
    for bad in ({}, [], {"items": "x"}):
        with pytest.raises(NewsError):
            bd2_news.fetch_bd2_news(get_json=lambda url, _bad=bad, **_: _bad)
    out = bd2_news.fetch_bd2_news(
        get_json=lambda url, **_: bd2_payload([bd2_row("../x"), bd2_row("ok")])
    )
    assert [i["id"] for i in out.items] == ["ok"]


# --------------------------------------------------------------------------- Endfield
COVER_E = "https://web-static.hg-cdn.com/upload/image/20261012/e95.jpg"


def ef_payload(rows):
    return {"code": 0, "msg": "", "data": {"list": rows}}


def ef_row(cid="9560", tab="notices", title="Version Update Notes", at=1792026000, cover=COVER_E):
    return {
        "cid": cid,
        "tab": tab,
        "sticky": False,
        "title": title,
        "displayTime": at,
        "cover": cover,
        "brief": "Dear Endministrator",
    }


def test_endfield_item_fields():
    out = endfield_news.fetch_endfield_news(get_json=lambda url, **_: ef_payload([ef_row()]))
    item = out.items[0]
    assert item["id"] == "9560" and item["category"] == "Notices" and item["image"] == COVER_E
    assert item["published_at"] == 1792026000000 and item["summary"] == "Dear Endministrator"
    assert item["url"] == "https://endfield.gryphline.com/en-us/news/9560"


def test_endfield_merges_tabs_dedupes_and_sorts():
    def get(url, **_):
        if "tabs[]=events" in url:
            return ef_payload([ef_row("2", "events", at=300), ef_row("1", "events", at=100)])
        if "tabs[]=notices" in url:
            return ef_payload([ef_row("1", "notices", at=100)])
        return ef_payload([ef_row("3", "news", at=200)])

    out = endfield_news.fetch_endfield_news(get_json=get)
    assert [i["id"] for i in out.items] == ["2", "3", "1"]


def test_endfield_one_failing_tab_is_partial_all_failing_is_an_error():
    def one_bad(url, **_):
        if "tabs[]=news" in url:
            raise NewsError("503")
        return ef_payload([ef_row(cid=url[-3:] or "x")])

    out = endfield_news.fetch_endfield_news(get_json=one_bad)
    assert out.partial and out.items

    def all_bad(url, **_):
        raise NewsError("503")

    with pytest.raises(NewsError):
        endfield_news.fetch_endfield_news(get_json=all_bad)


def test_endfield_untrusted_cover_is_dropped_and_bad_envelope_raises():
    out = endfield_news.fetch_endfield_news(
        get_json=lambda url, **_: ef_payload([ef_row(cover="https://evil.example/a.jpg")])
    )
    assert out.items[0]["image"] is None
    with pytest.raises(NewsError):
        endfield_news.fetch_endfield_news(get_json=lambda url, **_: {"code": 7})


# --------------------------------------------------------------------------- wiring
@pytest.mark.parametrize(
    ("kind", "source"),
    [
        ("direct-wuwa", kuro_news.fetch_wuwa_news),
        ("direct-pgr", kuro_news.fetch_pgr_news),
        ("direct-bd2", bd2_news.fetch_bd2_news),
        ("direct-endfield", endfield_news.fetch_endfield_news),
        ("direct-nte", nte_news.fetch_nte_news),
        ("launcher-nte", nte_news.fetch_nte_news),
        ("launcher-nikke", nikke_news.fetch_nikke_news),
    ],
)
def test_backend_is_wired_to_its_news_source(kind, source):
    from nadare.core import backends

    cls = backends.get(kind)
    assert cls.news_source is source
