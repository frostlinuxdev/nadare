# SPDX-License-Identifier: GPL-3.0-or-later
"""#1536: the Store has no detail page; a card goes straight to the Install dialog.

Before: Store -> card -> detail page -> Install -> "Install X?" dialog. Two confirmations for one
install. Now a card opens the dialog, and the dialog carries what only the detail page showed:
the tier, the delivery line, the game's notice with its link, and the version picker.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from nadare.core import artwork, steamstore
from nadare.core import backends as core_backends
from nadare.core.config import Config
from nadare.core.storecatalog import game_of
from nadare.gui import quick_install
from nadare.gui.main_window import PAGE_GAME, MainWindow
from nadare.gui.quick_install import QuickInstallDialog, StoreFacts, facts_for
from nadare.gui.store_flow import StoreFlow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _window(tmp_path, **profiles):
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles.items():
        config.add_profile(name, **values)
    config.save()
    return MainWindow(config)


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _describe(_chosen):
    return "/games", "100 GiB free", ""


# ---- the flow: no page in between ------------------------------------------------------------
def test_the_store_has_no_detail_page(qapp):
    flow = StoreFlow()
    try:
        assert flow.count() == 1 and flow.currentWidget() is flow.grid
        assert not hasattr(flow, "show_detail")
    finally:
        flow.deleteLater()


def test_a_card_for_a_game_not_installed_asks_to_install_it(qapp):
    flow = StoreFlow()
    asked: list[str] = []
    played: list[str] = []
    flow.install_requested.connect(asked.append)
    flow.play_requested.connect(played.append)
    try:
        flow.grid.entry_activated.emit("direct-pgr")
        assert asked == ["direct-pgr"] and played == []
        assert flow.currentWidget() is flow.grid
    finally:
        flow.deleteLater()


def test_a_card_for_an_installed_game_plays_it(qapp):
    flow = StoreFlow()
    asked: list[str] = []
    played: list[str] = []
    flow.install_requested.connect(asked.append)
    flow.play_requested.connect(played.append)
    try:
        flow.set_installed_kinds(frozenset({"direct-pgr"}))
        flow.grid.entry_activated.emit("direct-pgr")
        assert played == ["direct-pgr"] and asked == []
    finally:
        flow.deleteLater()


def test_a_grouped_card_plays_the_version_that_is_installed(qapp):
    flow = StoreFlow()
    played: list[str] = []
    flow.play_requested.connect(played.append)
    try:
        flow.set_installed_kinds(frozenset({"direct-gfl2-haoplay"}))
        flow.grid.entry_activated.emit("direct-gfl2")  # the card stands for the whole game
        assert played == ["direct-gfl2-haoplay"]
    finally:
        flow.deleteLater()


def test_a_steam_tile_goes_to_its_own_flow_installed_or_not(qapp):
    from nadare.core.storecatalog import entries

    kind = next((e.kind for e in entries() if steamstore.app_id_of(e.kind) is not None), None)
    if kind is None:
        pytest.skip("no Steam tile in this catalog")
    flow = StoreFlow()
    asked: list[str] = []
    flow.install_requested.connect(asked.append)
    try:
        flow.set_installed_kinds(frozenset({kind}))
        flow.grid.entry_activated.emit(kind)
        assert asked == [kind]  # the window adds it or reopens it ("Open")
    finally:
        flow.deleteLater()


# ---- the dialog carries the facts --------------------------------------------------------------
def test_the_dialog_shows_tier_delivery_and_the_notice_with_its_link(qapp, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(quick_install.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    facts = StoreFacts(
        tier="Experimental",
        tier_help="New in Nadare.",
        delivery="Direct download",
        notice=("Keep 60 GB free.", "https://example.invalid/dna", "Official site"),
    )
    dialog = QuickInstallDialog("Duet Night Abyss", notice="", describe=_describe, facts=facts)
    try:
        assert dialog.tier_label.text() == "Experimental"
        assert dialog.tier_help_label.text() == "New in Nadare."
        assert dialog.delivery_label.text().startswith("Direct download")
        assert "without their launcher" in dialog.delivery_label.text()
        assert dialog.page_notice_label.text() == "Keep 60 GB free."
        assert not dialog.page_notice_label.isHidden()
        assert dialog.page_notice_link.text() == "Official site"
        dialog.page_notice_link.click()
        assert opened == ["https://example.invalid/dna"]
    finally:
        dialog.deleteLater()


def test_a_dialog_without_facts_looks_as_before(qapp):
    dialog = QuickInstallDialog("Game", notice="", describe=_describe)
    try:
        assert dialog.tier_label.isHidden() and dialog.page_notice_label.isHidden()
        assert dialog.variant_combo is None
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("kind", ["direct-dna", "direct-snowbreak", "launcher-nikke"])
def test_facts_for_a_catalog_game_carry_its_page_notice(kind):
    facts = facts_for(kind)
    notice = core_backends.page_notice(kind)
    assert notice, f"{kind} has a page notice"
    assert facts.notice is not None and facts.notice[0] == notice[0]
    assert facts.tier in ("Verified", "Experimental")
    assert facts.delivery


def test_the_version_picker_lists_the_builds_and_reports_a_change(qapp):
    game = game_of("direct-gfl2")
    variants = [(e.variant or e.title, e.kind) for e in game.entries]
    dialog = QuickInstallDialog(
        "Girls' Frontline 2", notice="", describe=_describe,
        variants=variants, current_kind="direct-gfl2",
    )
    try:
        combo = dialog.variant_combo
        assert combo is not None and combo.count() == 2
        assert combo.currentData() == "direct-gfl2"
        combo.setCurrentIndex(1)
        assert dialog.result() == quick_install.VARIANT_CHANGED
        assert dialog.variant_kind == "direct-gfl2-haoplay"
    finally:
        dialog.deleteLater()


# ---- the window --------------------------------------------------------------------------------
@pytest.fixture
def dialogs(monkeypatch):
    class State:
        shown: list = []
        script: list = []  # per exec: "accept", "reject" or ("variant", kind)

    def fake_exec(self):
        State.shown.append(self)
        step = State.script.pop(0) if State.script else "reject"
        if isinstance(step, tuple):
            self.variant_kind = step[1]
            return quick_install.VARIANT_CHANGED
        return QDialog.DialogCode.Accepted if step == "accept" else QDialog.DialogCode.Rejected

    State.shown, State.script = [], []
    monkeypatch.setattr(QuickInstallDialog, "exec", fake_exec)
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    return State


def test_a_card_click_in_the_window_opens_the_dialog_with_the_games_notice(
    tmp_path, qapp, dialogs
):
    window = _window(tmp_path)
    try:
        window.show_store()
        window.store.grid.entry_activated.emit("direct-snowbreak")
        assert len(dialogs.shown) == 1
        shown = dialogs.shown[0]
        assert shown.page_notice_label.text() == core_backends.page_notice("direct-snowbreak")[0]
        assert window.config.profiles == {}  # cancelled: nothing added
    finally:
        _close(window, qapp)


def test_picking_another_version_reopens_the_dialog_for_it(tmp_path, qapp, dialogs):
    window = _window(tmp_path)
    try:
        window.show_store()
        dialogs.script = [("variant", "direct-gfl2-haoplay"), "reject"]
        window.store.grid.entry_activated.emit("direct-gfl2")
        assert len(dialogs.shown) == 2
        assert dialogs.shown[1].variant_combo.currentData() == "direct-gfl2-haoplay"
        assert window.config.profiles == {}
    finally:
        _close(window, qapp)


def test_a_card_for_an_installed_game_opens_its_page(tmp_path, qapp, dialogs):
    window = _window(tmp_path, pgr={"backend": "direct-pgr", "title": "Punishing"})
    try:
        window.show_store()
        window.store.grid.entry_activated.emit("direct-pgr")
        assert window.stack.currentIndex() == PAGE_GAME
        assert dialogs.shown == []
    finally:
        _close(window, qapp)


def test_install_has_the_focus_not_the_notice_link(qapp):
    facts = StoreFacts("Verified", "", "Direct download", ("Note.", "https://example.invalid"))
    dialog = QuickInstallDialog("Game", notice="", describe=_describe, facts=facts)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.focusWidget() is dialog.install_button
    finally:
        dialog.close()
        dialog.deleteLater()
