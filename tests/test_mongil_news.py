# SPDX-License-Identifier: GPL-3.0-or-later
"""#535: Mongil: Star Dive news from the official Netmarble forum's public JSON API.

Probed 2026-11-08, anonymous GET, no cookies:
``https://forum.netmarble.com/api/game/monster2/official/forum/stardive_gl/article/list
?menuSeq=<board>&viewType=lv&rows=12&start=0&languageCd=en_US`` ->
``{code: 0, articleList: [{id, menuSeq, title, regDate (ms), thumbnailUrl,
managerLanguageCds: [...]}]}``. Boards: 2 Notice, 8 Update, 6 Events. One list mixes the
Thai, Traditional Chinese and English posts; ``managerLanguageCds`` says which is which.
Articles open at ``https://forum.netmarble.com/stardive_gl/view/<board>/<id>``.
"""

from __future__ import annotations

import pytest

from nadare.backends import mongil_news
from nadare.core.news import NewsError

THUMB = "https://hedwig-cf.netmarble.com/forum-common/monster2/stardive_gl/thumbnail/a_lp.jpg"
EN = ["en_US", "zh_CN", "fr_FR"]


def art(aid, board=2, title="Maintenance Notice", when=1793599211564, langs=EN, thumb=THUMB):
    return {"id": aid, "menuSeq": board, "title": title, "regDate": when,
            "thumbnailUrl": thumb, "managerLanguageCds": langs}


def routes(boards, fail=()):
    def get(url, **_):
        for board, rows in boards.items():
            if url == mongil_news.board_url(board):
                if board in fail:
                    raise NewsError("board down")
                return {"code": 0, "msg": "OK", "articleList": rows, "totalCount": len(rows)}
        raise NewsError(f"unexpected {url}")

    return get


def test_item_fields():
    out = mongil_news.fetch_mongil_news(get_json=routes({2: [art(737)], 8: [], 6: []}))
    (item,) = out.items
    assert item["id"] == "737"
    assert item["title"] == "Maintenance Notice"
    assert item["category"] == "Notice"
    assert item["url"] == "https://forum.netmarble.com/stardive_gl/view/2/737"
    assert item["image"] == THUMB
    assert item["published_at"] == 1793599211564


def test_only_english_posts_are_kept():
    rows = [art(745, langs=["th_TH"], title="ประกาศ"), art(743, langs=["zh_TW"]), art(737)]
    out = mongil_news.fetch_mongil_news(get_json=routes({2: rows, 8: [], 6: []}))
    assert [item["id"] for item in out.items] == ["737"]


def test_boards_merge_newest_first_deduped_and_capped():
    boards = {
        2: [art(i, board=2, when=1_000 + i) for i in range(1, 9)],
        8: [art(100 + i, board=8, when=2_000 + i) for i in range(1, 9)],
        6: [art(200 + i, board=6, when=3_000 + i) for i in range(1, 9)] + [art(201, board=6)],
    }
    out = mongil_news.fetch_mongil_news(get_json=routes(boards))
    ids = [item["id"] for item in out.items]
    assert len(ids) == len(set(ids)) == mongil_news.MAX_ITEMS
    assert out.items[0]["category"] in ("Events", "Notice")


def test_one_failing_board_is_partial_all_failing_is_an_error():
    out = mongil_news.fetch_mongil_news(get_json=routes({2: [art(1)], 8: [], 6: []}, fail=(8,)))
    assert out.partial and len(out.items) == 1
    with pytest.raises(NewsError):
        mongil_news.fetch_mongil_news(get_json=routes({2: [], 8: [], 6: []}, fail=(2, 8, 6)))


def test_untrusted_thumbnails_and_bad_rows_are_dropped():
    rows = [art(1, thumb="https://evil.example/x.jpg"), art(None), art(2, title=""),
            art("../x"), "junk"]
    out = mongil_news.fetch_mongil_news(get_json=routes({2: rows, 8: [], 6: []}))
    assert [item["id"] for item in out.items] == ["1"]
    assert out.items[0]["image"] is None


def test_a_bad_envelope_counts_as_a_failed_board():
    def get(url, **_):
        return {"code": 7}

    with pytest.raises(NewsError):
        mongil_news.fetch_mongil_news(get_json=get)


def test_the_mongil_backend_uses_it():
    from nadare.core import backends as core_backends

    cls = core_backends.get("direct-mongil")
    assert cls.news_source is not None
    assert cls.official_image_hosts == mongil_news.IMAGE_HOSTS
