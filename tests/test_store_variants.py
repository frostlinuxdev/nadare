# SPDX-License-Identifier: GPL-3.0-or-later
"""One Store tile per game; the builds of a game are a version picker on its page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.storecatalog import entries, game_of, store_games
from nadare.gui.store import StorePage
from nadare.gui.store_detail import StoreDetail
from nadare.gui.store_flow import StoreFlow

GFL2 = {"direct-gfl2", "direct-gfl2-haoplay", "launcher-gfl2-haoplay"}
NTE = {"direct-nte", "launcher-nte"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)


def _by_kind():
    return {entry.kind: entry for entry in entries()}


# ---- the headless model -------------------------------------------------------------------
def test_the_builds_of_one_game_share_a_group_and_say_what_they_are():
    by_kind = _by_kind()
    assert {by_kind[k].group for k in GFL2} == {"Girls' Frontline 2"}
    assert {by_kind[k].group for k in NTE} == {"Neverness to Everness"}
    assert all(by_kind[k].variant for k in GFL2 | NTE)
    assert len({by_kind[k].variant for k in GFL2}) == 3


def test_a_game_with_one_build_has_no_group_and_no_variant_label():
    by_kind = _by_kind()
    assert by_kind["direct-pgr"].group == "" and by_kind["direct-pgr"].variant == ""


def test_store_games_has_one_game_per_title():
    games = store_games()
    assert len(games) == len(entries()) - 2 - 1  # GFL2: 3 -> 1, NTE: 2 -> 1
    titles = [game.title for game in games]
    assert titles.count("Girls' Frontline 2") == 1
    assert titles.count("Neverness to Everness") == 1
    assert len(set(titles)) == len(titles)


def test_the_first_build_of_a_game_is_the_most_supported_one():
    gfl2 = next(g for g in store_games() if g.title == "Girls' Frontline 2")
    assert {e.kind for e in gfl2.entries} == GFL2
    assert gfl2.primary.kind == "direct-gfl2"  # Verified, so it leads
    nte = next(g for g in store_games() if g.title == "Neverness to Everness")
    assert nte.primary.kind == "direct-nte"


def test_a_single_build_game_keeps_its_own_title():
    pgr = next(g for g in store_games() if g.primary.kind == "direct-pgr")
    assert pgr.title == "Punishing: Gray Raven" and len(pgr.entries) == 1


def test_search_finds_a_game_by_its_group_name_and_by_a_build_name():
    by_group = store_games(query="girls")
    assert [g.title for g in by_group] == ["Girls' Frontline 2"]
    assert {e.kind for e in by_group[0].entries} == GFL2
    by_build = store_games(query="haoplay")
    assert [g.title for g in by_build] == ["Girls' Frontline 2"]
    assert {e.kind for e in by_build[0].entries} == {"direct-gfl2-haoplay", "launcher-gfl2-haoplay"}


def test_the_tier_filter_keeps_only_matching_builds_inside_a_game():
    verified = store_games(tier="Verified")
    for game in verified:
        assert all(e.tier == "Verified" for e in game.entries)


def test_a_game_is_installed_when_any_of_its_builds_is():
    game = next(
        g
        for g in store_games(installed_kinds=frozenset({"launcher-nte"}))
        if g.title == "Neverness to Everness"
    )
    assert game.installed is True


def test_game_of_finds_the_whole_group_from_any_build():
    for kind in GFL2:
        game = game_of(kind)
        assert game is not None and {e.kind for e in game.entries} == GFL2
    assert game_of("no-such-kind") is None


# ---- the grid -----------------------------------------------------------------------------
def _shown_page(qapp) -> StorePage:
    page = StorePage()
    page.resize(1400, 900)
    page.show()
    qapp.processEvents()
    return page


def test_the_grid_has_one_card_per_game(qapp):
    page = _shown_page(qapp)
    try:
        assert len(page.visible_kinds()) == len(store_games())
        assert "direct-gfl2" in page.visible_kinds()
        assert "direct-gfl2-haoplay" not in page.visible_kinds()
        assert "launcher-nte" not in page.visible_kinds()
    finally:
        page.close()


def test_a_grouped_card_shows_the_game_name_and_how_many_versions(qapp):
    page = _shown_page(qapp)
    try:
        card = page._cards["direct-gfl2"]
        assert card.title_label.text() == "Girls' Frontline 2"
        assert "3 versions" in card.delivery_label.text()
        assert card.activated is not None
        seen = []
        card.activated.connect(seen.append)
        card.activated.emit(card.entry.kind)
        assert seen == ["direct-gfl2"]
    finally:
        page.close()


def test_focus_returns_to_the_card_of_the_game_even_for_another_build(qapp):
    page = _shown_page(qapp)
    try:
        page.focus_card("launcher-gfl2-haoplay")
        assert page.focusWidget() is page._cards["direct-gfl2"]
    finally:
        page.close()


def test_a_grouped_card_still_fetches_art_by_the_real_title(qapp, monkeypatch):
    asked = []
    monkeypatch.setattr(
        artwork,
        "fetch",
        lambda profile, title=None, kind="cover": asked.append((profile["backend"], title)),
    )
    page = _shown_page(qapp)
    try:
        page.wait_for_images()
        assert ("direct-gfl2", "Girls' Frontline 2: Exilium (Darkwinter build)") in asked
    finally:
        page.close()


# ---- the detail page ----------------------------------------------------------------------
def test_the_detail_page_offers_a_version_picker_for_a_game_with_several_builds(qapp):
    game = game_of("direct-gfl2")
    detail = StoreDetail(game.primary)
    detail.set_entry(game.primary, game.entries)
    try:
        assert not detail.variant_box.isHidden()
        assert detail.variant_box.count() == 3
        seen = []
        detail.variant_requested.connect(seen.append)
        detail.variant_box.setCurrentIndex(1)
        assert seen == [game.entries[1].kind]
    finally:
        detail.deleteLater()


def test_a_single_build_game_has_no_version_picker(qapp):
    game = next(g for g in store_games() if g.primary.kind == "direct-pgr")
    detail = StoreDetail(game.primary)
    try:
        assert detail.variant_box.isHidden()
    finally:
        detail.deleteLater()


def test_the_flow_shows_the_chosen_build_and_keeps_the_picker_on_it(qapp):
    flow = StoreFlow()
    try:
        flow.show_detail("direct-gfl2")
        flow.detail.variant_box.setCurrentIndex(2)
        qapp.processEvents()
        chosen = game_of("direct-gfl2").entries[2]
        assert flow.detail.entry.kind == chosen.kind
        assert flow.detail.variant_box.currentIndex() == 2
        assert flow.detail.install_button.isEnabled()
        got = []
        flow.install_requested.connect(got.append)
        flow.detail.install_button.click()
        assert got == [chosen.kind]
    finally:
        flow.deleteLater()


def test_a_grouped_card_stays_marked_added_when_a_filter_hides_the_installed_build(qapp):
    page = StorePage()
    page.resize(1400, 900)
    page.show()
    qapp.processEvents()
    try:
        page.set_installed_kinds(frozenset({"direct-gfl2"}))
        page.search.setText("haoplay")  # the Darkwinter build is hidden by the search
        qapp.processEvents()
        card = page._cards["direct-gfl2-haoplay"]
        assert not card.added_marker.isHidden()
        page.set_installed_kinds(frozenset())
        assert card.added_marker.isHidden()
    finally:
        page.close()
