# SPDX-License-Identifier: GPL-3.0-or-later
"""The index carries two shapes of art: a portrait card cover and wide key art."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import artindex

PORTRAIT = "https://cdn2.unrealengine.com/portrait.jpg"
WIDE = "https://cdn2.unrealengine.com/wide.jpg"
SEED = Path(__file__).resolve().parents[1] / "docs" / "art-index.seed.json"


def _doc(**games):
    return {"schema": 1, "updated": "2026-11-13", "games": games}


def _entry(url, orientation, width, height):
    return {"url": url, "orientation": orientation, "width": width, "height": height}


@pytest.fixture(autouse=True)
def fresh():
    artindex.reset()
    yield
    artindex.reset()


def test_cover_and_wide_are_parsed_into_separate_candidates():
    doc = _doc(
        **{
            "direct-genshin": {
                "cover": [_entry(PORTRAIT, "portrait", 1200, 1600)],
                "wide": [_entry(WIDE, "wide", 2560, 1440)],
            }
        }
    )
    cover = artindex.parse(doc)["direct-genshin"]
    wide = artindex.parse_wide(doc)["direct-genshin"]
    assert [(c.url, c.orientation) for c in cover] == [(PORTRAIT, "portrait")]
    assert [(c.url, c.orientation) for c in wide] == [(WIDE, "wide")]


def test_wide_lookup_waits_and_returns_the_wide_candidates():
    doc = _doc(
        **{
            "direct-genshin": {
                "cover": [_entry(PORTRAIT, "portrait", 1200, 1600)],
                "wide": [_entry(WIDE, "wide", 2560, 1440)],
            }
        }
    )
    artindex.set_loader(lambda: doc)
    assert artindex.lookup("direct-genshin", art="wide", wait=True) == [
        (WIDE, ("cdn2.unrealengine.com",))
    ]
    assert artindex.lookup("direct-genshin", wait=True) == [
        (PORTRAIT, ("cdn2.unrealengine.com",))
    ]


def test_a_wide_field_that_is_not_a_list_is_a_broken_document():
    doc = _doc(**{"direct-genshin": {"cover": [], "wide": "not-a-list"}})
    assert artindex.valid_document(doc) is False
    assert artindex.parse(doc) == {} and artindex.parse_wide(doc) == {}


def test_an_unknown_art_kind_is_ignored():
    assert artindex.lookup("direct-genshin", art="square", wait=True) == []


def test_the_seed_has_portrait_covers_and_wide_art_where_the_publisher_has_them():
    doc = json.loads(SEED.read_text(encoding="utf-8"))
    cover = artindex.parse(doc)
    wide = artindex.parse_wide(doc)
    # Steam-backed games publish a proper 2:3 library capsule: the card no longer has to
    # letterbox a cinemascope hero.
    for kind in (
        "direct-wuwa",
        "direct-pgr",
        "direct-gfl2",
        "direct-gfl2-haoplay",
        "direct-zzz",
        "direct-endfield",
        "stellasora-yostar",
        "direct-nte",
    ):
        assert cover[kind][0].orientation == "portrait", kind
        assert cover[kind][0].height > cover[kind][0].width, kind
        assert wide[kind][0].orientation == "wide", kind
        assert wide[kind][0].width > wide[kind][0].height, kind
    # HoYoverse's Epic press art includes a portrait poster for Genshin, HSR and HI3.
    for kind in ("direct-genshin", "direct-hsr", "direct-hi3"):
        assert cover[kind][0].orientation == "portrait", kind
        assert wide[kind][0].orientation == "wide", kind
    # NIKKE's mobile key art is the portrait cover; its PC key art is the wide one.
    assert cover["launcher-nikke"][0].orientation == "portrait"
    assert wide["launcher-nikke"][0].orientation == "wide"
    # The app reads at most four candidates per kind.
    assert all(len(entries) <= artindex.MAX_CANDIDATES for entries in cover.values())
    assert all(len(entries) <= artindex.MAX_CANDIDATES for entries in wide.values())


def test_the_seed_still_has_a_cover_for_every_game():
    doc = json.loads(SEED.read_text(encoding="utf-8"))
    parsed = artindex.parse(doc)
    for kind in doc["games"]:
        assert parsed.get(kind), f"{kind}: no acceptable cover"


@pytest.mark.parametrize("art", ["cover", "wide"])
def test_steam_asset_hosts_are_limited_to_steam_hosted_games(art):
    for kind, hosts in artindex.ALLOWED_HOSTS.items():
        doc = _doc(**{kind: {art: [_entry(
            "https://shared.akamai.steamstatic.com/store_item_assets/art.jpg", "wide", 1600, 640
        )]}})
        parsed = artindex.parse(doc) if art == "cover" else artindex.parse_wide(doc)
        assert bool(parsed.get(kind)) == ("shared.akamai.steamstatic.com" in hosts)


def test_cover_only_documents_still_answer_without_wide_art():
    doc = _doc(**{"direct-genshin": {"cover": [_entry(PORTRAIT, "portrait", 600, 900)]}})
    artindex.set_loader(lambda: doc)
    assert artindex.valid_document(doc)
    assert artindex.lookup("direct-genshin", art="wide", wait=True) == []
    assert artindex.lookup("direct-genshin", wait=True) == [(PORTRAIT, ("cdn2.unrealengine.com",))]


@pytest.mark.parametrize("kind", ["hbr-yostar", "azurpromilia-jp-yostar"])
def test_the_seed_has_art_for_the_yostar_games_without_a_news_feed(kind):
    # HBR and Azur Promilia have no news feed to borrow a cover from, so without a seed entry
    # their store card stayed on the placeholder (release prep 11-27).
    doc = json.loads(SEED.read_text(encoding="utf-8"))
    cover = artindex.parse(doc)
    wide = artindex.parse_wide(doc)
    assert cover.get(kind), kind
    assert wide.get(kind), kind
    assert wide[kind][0].width > wide[kind][0].height, kind
