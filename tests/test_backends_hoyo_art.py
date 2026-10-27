# SPDX-License-Identifier: GPL-3.0-or-later
"""HoYoverse launcher art (hyp-connect getGames): parsing and host allowlists."""

from __future__ import annotations

import pytest

from nadare.backends import hoyo_art
from nadare.core.news import NewsError

BG = "https://fastcdn.hoyoverse.com/static-resource-v2/2026/09/12/bg.webp"
THUMB = "https://launcher-webstatic.hoyoverse.com/launcher-public/2026/03/27/thumb.png"


def game(game_id="5TIVvvcwtM", background=BG, thumbnail=THUMB):
    return {
        "id": game_id,
        "biz": "bh3_global",
        "display": {
            "background": {"url": background, "link": "", "login_state_in_link": False}
            if isinstance(background, str)
            else background,
            "thumbnail": {"url": thumbnail, "link": "", "login_state_in_link": False}
            if isinstance(thumbnail, str)
            else thumbnail,
        },
    }


def payload(*games):
    return {"retcode": 0, "message": "OK", "data": {"games": list(games)}}


def fetch(*games, game_id="5TIVvvcwtM"):
    seen = []

    def get_json(url, **kwargs):
        seen.append(url)
        return payload(*games)

    return hoyo_art.fetch_art(game_id, get_json), seen


def test_maps_background_to_hero_and_thumbnail_to_cover() -> None:
    art, seen = fetch(game("x1"), game("5TIVvvcwtM"))
    assert art == {"hero": BG, "cover": THUMB}
    assert seen == [
        "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGames"
        "?launcher_id=VYTpXlbWo8&language=en-us"
    ]


def test_picks_only_the_requested_game() -> None:
    other = game("gopR6Cufr3", background="https://fastcdn.hoyoverse.com/other.webp")
    art, _ = fetch(other, game("5TIVvvcwtM"), game_id="5TIVvvcwtM")
    assert art["hero"] == BG


def test_an_unknown_game_gives_nothing() -> None:
    art, _ = fetch(game("gopR6Cufr3"), game_id="5TIVvvcwtM")
    assert art == {}


@pytest.mark.parametrize(
    "bad",
    [
        "http://fastcdn.hoyoverse.com/a.webp",
        "https://evil.example/a.webp",
        "https://fastcdn.hoyoverse.com.evil.example/a.webp",
        "https://user:pw@fastcdn.hoyoverse.com/a.webp",
        "https://fastcdn.hoyoverse.com:8443/a.webp",
        "",
        None,
        42,
    ],
)
def test_images_off_the_official_cdns_are_dropped(bad) -> None:
    art, _ = fetch(game(background=bad, thumbnail=bad))
    assert art == {}


def test_one_bad_field_does_not_drop_the_other() -> None:
    art, _ = fetch(game(background="https://evil.example/a.webp"))
    assert art == {"cover": THUMB}


def test_a_field_with_only_a_url_is_accepted() -> None:
    art, _ = fetch(game(background=BG, thumbnail={"url": THUMB}))
    assert art == {"hero": BG, "cover": THUMB}


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        [],
        {"retcode": -1, "data": {"games": []}},
        {"retcode": 0, "data": None},
        {"retcode": 0, "data": {"games": "nope"}},
    ],
)
def test_a_bad_envelope_raises(envelope) -> None:
    with pytest.raises(NewsError):
        hoyo_art.fetch_art("5TIVvvcwtM", lambda url, **kw: envelope)


def test_junk_game_rows_are_skipped() -> None:
    art, _ = fetch("junk", 7, {"id": "other", "display": None}, game("5TIVvvcwtM"))
    assert art == {"hero": BG, "cover": THUMB}


def test_a_matching_game_without_display_gives_nothing() -> None:
    art, _ = fetch({"id": "5TIVvvcwtM", "display": None})
    assert art == {}
