# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.diskspace import human_size
from nadare.core.storecatalog import entries
from nadare.gui import store
from nadare.gui.images import ImageLoader
from nadare.gui.store import StoreCard, StorePage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *_args, **_kwargs: None)


def _shown_page(qapp, **kwargs) -> StorePage:
    page = StorePage(**kwargs)
    page.resize(900, 800)
    page.show()
    qapp.processEvents()
    return page


def test_store_grid_card_count_matches_model(qapp):
    page = _shown_page(qapp)
    try:
        assert len(page.visible_kinds()) == len(entries())
    finally:
        page.close()


def test_search_is_case_insensitive_and_clear_button_restores_cards(qapp):
    page = _shown_page(qapp)
    try:
        count = len(page.visible_kinds())
        page.search.setText("GRAY")
        qapp.processEvents()
        assert page.visible_kinds() == ["direct-pgr"]
        page.search.clear()
        qapp.processEvents()
        assert len(page.visible_kinds()) == count
    finally:
        page.close()


def test_tier_filter_updates_cards(qapp):
    page = _shown_page(qapp)
    try:
        page.tier_filter.setCurrentText("Verified")
        qapp.processEvents()
        assert page.visible_kinds() == [entry.kind for entry in entries(tier="Verified")]
    finally:
        page.close()


def test_added_marker_updates_with_installed_kinds(qapp):
    page = _shown_page(qapp)
    try:
        card = page._cards["direct-pgr"]
        assert not card.added_marker.isVisible()
        page.set_installed_kinds(frozenset({"direct-pgr"}))
        qapp.processEvents()
        assert card.added_marker.text() == "Added"
        assert card.added_marker.isVisible()
        page.set_installed_kinds(frozenset())
        assert not card.added_marker.isVisible()
    finally:
        page.close()


def test_card_shows_tier_delivery_and_optional_size(qapp):
    entry = next(item for item in entries() if item.kind == "direct-pgr")
    card = StoreCard(entry)
    try:
        assert card.tier_badge.text() == entry.tier
        assert card.tier_badge.toolTip() == entry.tier_help
        assert card.delivery_label.text() == entry.delivery_label
        assert card.size_label.text() == f"~{human_size(entry.size_bytes)}"

        no_size = StoreCard(replace(entry, size_bytes=None))
        try:
            assert no_size.size_label.text() == ""
            assert no_size.size_label.isHidden()
        finally:
            no_size.deleteLater()
    finally:
        card.deleteLater()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space])
def test_store_card_keyboard_activation_emits_kind(qapp, key):
    page = _shown_page(qapp)
    try:
        card = page._cards["direct-pgr"]
        received = []
        page.entry_activated.connect(received.append)
        card.setFocus()
        QTest.keyClick(card, key)
        assert received == ["direct-pgr"]
    finally:
        page.close()


def test_store_card_click_emits_kind(qapp):
    page = _shown_page(qapp)
    try:
        card = page._cards["direct-pgr"]
        received = []
        page.entry_activated.connect(received.append)
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        assert received == ["direct-pgr"]
    finally:
        page.close()


def test_clicking_card_content_activates_it(qapp):
    page = _shown_page(qapp)
    try:
        card = page._cards["direct-pgr"]
        received = []
        page.entry_activated.connect(received.append)
        QTest.mouseClick(card.title_label, Qt.MouseButton.LeftButton)
        assert received == ["direct-pgr"]
    finally:
        page.close()


def test_empty_state_is_shown_when_nothing_matches(qapp):
    page = _shown_page(qapp)
    try:
        page.search.setText("no such game")
        qapp.processEvents()
        assert page.visible_kinds() == []
        assert page.empty_label.isVisible()
        assert page.empty_label.text() == "No games match your search."
    finally:
        page.close()


def test_tab_order_reaches_search_filter_then_cards(qapp):
    page = _shown_page(qapp)
    try:
        first = page._cards[page.visible_kinds()[0]]
        page.search.setFocus()
        QTest.keyClick(page.search, Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is page.tier_filter
        QTest.keyClick(page.tier_filter, Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is first
    finally:
        page.close()


def test_construction_does_not_fetch_art(monkeypatch, qapp):
    def fail(*_args, **_kwargs):
        raise AssertionError("artwork fetch must wait until the page is shown")

    monkeypatch.setattr(artwork, "fetch", fail)
    monkeypatch.setattr(store, "entries", fail)
    page = StorePage()
    try:
        assert page.visible_kinds() == []
        assert not page._built
        assert not page._art_started
    finally:
        page.close()


def test_art_is_fetched_in_background_and_decoded_by_image_loader(
    monkeypatch, qapp, tmp_path
):
    art_path = Path(tmp_path) / "art.png"
    image = QImage(120, 180, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ff00aa"))
    assert image.save(str(art_path))
    called = []
    real_request = ImageLoader.request

    def fetch(profile, title=None, kind="cover"):
        called.append((profile, title, kind))
        return art_path

    requests = []

    def request(self, *args, **kwargs):
        requests.append(args[0])
        return real_request(self, *args, **kwargs)

    monkeypatch.setattr(artwork, "fetch", fetch)
    monkeypatch.setattr(ImageLoader, "request", request)
    page = _shown_page(qapp)
    try:
        placeholder_key = page._cards["direct-pgr"].art.pixmap().cacheKey()
        page.wait_for_images()
        qapp.processEvents()
        assert called
        assert requests
        assert page._cards["direct-pgr"].art.pixmap().cacheKey() != placeholder_key
    finally:
        page.close()


@pytest.mark.parametrize("width", [520, 900, 1400])
def test_responsive_grid_has_no_horizontal_scrollbar(qapp, width):
    page = _shown_page(qapp)
    try:
        page.resize(width, 850)
        qapp.processEvents()
        assert page.scroll.horizontalScrollBar().maximum() == 0
    finally:
        page.close()


def _card_is_red(card) -> bool:
    image = card.art.pixmap().toImage()
    colour = QColor(image.pixel(image.width() // 2, image.height() // 2))
    return colour.red() > 200 and colour.green() < 60 and colour.blue() < 60


def test_card_art_survives_search_and_filter_changes(monkeypatch, qapp, tmp_path):
    art_path = Path(tmp_path) / "red.png"
    image = QImage(120, 180, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ff0000"))
    assert image.save(str(art_path))
    fetches = []

    def fetch(profile, title=None, kind="cover"):
        fetches.append(profile["backend"])
        return art_path

    monkeypatch.setattr(artwork, "fetch", fetch)
    page = _shown_page(qapp)
    try:
        page.wait_for_images()

        def with_art():
            return [card for card in page._cards.values() if card.entry.has_art]

        assert with_art() and all(_card_is_red(card) for card in with_art())
        fetched_once = sorted(fetches)

        for change in (
            lambda: page.search.setText("a"),
            lambda: page.search.setText(""),
            lambda: page.tier_filter.setCurrentText("Verified"),
            lambda: page.tier_filter.setCurrentText("All tiers"),
        ):
            change()
            page.wait_for_images()
            qapp.processEvents()
            assert with_art()
            assert all(_card_is_red(card) for card in with_art())

        assert sorted(fetches) == fetched_once  # rebuilt cards reuse the fetched art
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()
