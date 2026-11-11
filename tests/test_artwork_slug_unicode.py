# SPDX-License-Identifier: GPL-3.0-or-later
"""#622: titles in other scripts no longer share one ``artwork`` cache slug."""

from __future__ import annotations

from nadare.core.artwork import slug


def test_ascii_titles_keep_their_slug():
    assert slug({}, "Honkai: Star Rail") == "honkai-star-rail"
    assert slug({}, "NIKKE – Goddess of Victory") == "nikke-goddess-of-victory"  # punctuation only


def test_titles_in_other_scripts_get_distinct_stable_slugs():
    a = slug({}, "ブルーアーカイブ")
    b = slug({}, "原神")
    assert a != b
    assert a != "artwork" and b != "artwork"
    assert a == slug({}, "ブルーアーカイブ")  # stable across calls
    assert a.startswith("artwork-")


def test_a_mixed_title_does_not_collide_with_its_ascii_part():
    assert slug({}, "原神 Genshin") != slug({}, "Genshin")
    assert slug({}, "原神 Genshin").startswith("genshin-")


def test_slugs_stay_filename_safe():
    for title in ("ブルーアーカイブ", "原神 Genshin", "Café ☕", "../../etc"):
        value = slug({}, title)
        assert value and all(c.isascii() and (c.isalnum() or c == "-") for c in value)


def test_no_title_still_falls_back_to_artwork():
    assert slug({}) == "artwork"
