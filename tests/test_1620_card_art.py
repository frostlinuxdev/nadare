# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1620: the library card crops the cover to portrait around its centre, so the first cover
candidate must keep the game's name and key art in its middle."""

from __future__ import annotations

import json
from pathlib import Path

from nadare.core import artindex

SEED = Path(__file__).resolve().parents[1] / "docs" / "art-index.seed.json"


def _first(kind: str, art: str = "cover") -> str:
    games = json.loads(SEED.read_text("utf-8"))["games"]
    parsed = artindex.parse({"schema": 1, "games": {kind: games[kind]}})
    if art == "wide":
        parsed = artindex.parse_wide({"schema": 1, "games": {kind: games[kind]}})
    return parsed[kind][0].url


def test_mongil_cover_is_the_share_image_whose_middle_holds_the_wordmark():
    # The 1886x800 banner has the wordmark on its left third: a centre crop cut it off.
    assert _first("direct-mongil").endswith("/m3sm1772674620510.jpg")


def test_mongil_keeps_the_wide_banner_for_the_hero():
    assert _first("direct-mongil", "wide").endswith("2a3c4ed2-75bf-4267-93b9-a6cd32ab7b6e.png")


def test_nikke_cover_is_the_portrait_key_art_not_the_download_now_page_image():
    assert _first("launcher-nikke") == "https://nikke-en.com/m/ossweb-img/part1.jpg"
