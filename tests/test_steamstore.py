# SPDX-License-Identifier: GPL-3.0-or-later
"""Steam games in the Store: one tile per verified game, each with its own kind."""

from __future__ import annotations

from nadare.core import artwork, steamsetup, steamstore
from nadare.core.storecatalog import entries, game_of


def test_a_kind_names_the_game_and_back():
    assert steamstore.kind_for("5019180") == "steam-5019180"
    assert steamstore.app_id_of("steam-5019180") == "5019180"
    for bad in ("steam", "steam-", "steam-12x", "direct-bd2", "steam-5019180 ", ""):
        assert steamstore.app_id_of(bad) is None


def test_only_games_verified_on_a_real_steam_are_offered():
    offered = {game.app_id for game in steamstore.store_games()}
    assert offered == {game.app_id for game in steamsetup.GAMES if game.verified}
    assert {"5019180", "3224770", "3557620"} <= offered
    assert not {"2668080", "3308670", "3347400"} & offered


def test_the_store_lists_them_as_verified_steam_tiles():
    result = {entry.kind: entry for entry in entries()}
    epic = result["steam-5019180"]
    assert epic.title == "Epic Seven"
    assert epic.tier == "Verified"
    assert epic.delivery_label == "Through Steam"
    assert epic.has_art
    assert "steam-3557620" in result and "steam-3224770" in result
    assert "steam-2668080" not in result


def test_the_search_finds_a_steam_tile_by_title():
    assert [e.kind for e in entries(query="epic seven")] == ["steam-5019180"]


def test_a_steam_tile_is_installed_when_a_steam_profile_has_its_app_id():
    profiles = {
        "e7": {"backend": "steam", "steam_app_id": "5019180"},
        "extra": {"backend": "steam", "extra": {"steam_app_id": "3224770"}},
        "other": {"backend": "wine"},
        "bare": {"backend": "steam"},
    }
    kinds = steamstore.installed_kinds(profiles)
    assert kinds == {"steam", "wine", "steam-5019180", "steam-3224770"}
    shown = {e.kind: e.installed for e in entries(installed_kinds=frozenset(kinds))}
    assert shown["steam-5019180"] and shown["steam-3224770"] and not shown["steam-3557620"]


def test_a_steam_tile_is_its_own_game():
    game = game_of("steam-5019180")
    assert game is not None and game.title == "Epic Seven"
    assert [e.kind for e in game.entries] == ["steam-5019180"]


def test_the_cover_comes_from_that_games_steam_page():
    assert artwork._catalog_appid({"backend": "steam-5019180"}) == 5019180
    assert artwork._catalog_appid({"backend": "steam"}) is None


def test_the_profile_a_tile_creates():
    profile = steamstore.profile_for("steam-3557620")
    assert profile == {"backend": "steam", "steam_app_id": "3557620"}
    assert steamstore.profile_for("direct-bd2") is None
    assert steamstore.default_name("steam-3557620") == "Blue Archive"
