# SPDX-License-Identifier: GPL-3.0-or-later
"""Store polish: no repeated text, aligned cards, focus handling, and the minimum window."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork
from nadare.core.config import Config
from nadare.core.storecatalog import entries
from nadare.gui.main_window import MainWindow
from nadare.gui.store import StoreCard
from nadare.gui.store_detail import StoreDetail


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _entry(kind: str):
    found = {e.kind: e for e in entries()}
    if kind not in found:
        pytest.skip(f"{kind} is not available in this build")
    return found[kind]


def _window(tmp_path: Path) -> MainWindow:
    config = Config.load(tmp_path / "config.json")
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_container_page_says_its_requirements_once(qapp):
    widget = StoreDetail(_entry("fgo-lepton"))
    shown = " ".join(
        label.text().lower() for label in (widget.delivery_label, widget.notice_label)
    )
    assert shown.count("podman") == 1
    assert shown.count("binder") == 1
    assert "android container" in widget.delivery_label.text().lower()


def _top(card: StoreCard, widget) -> int:
    return widget.mapTo(card, QPoint(0, 0)).y()


def test_cards_line_up_whether_or_not_they_have_a_size_line(qapp):
    kinds = ("stove-czn", "arknights-yostar", "stellasora-yostar", "direct-bd2")
    cards = [StoreCard(_entry(kind)) for kind in kinds]
    assert any(card.entry.size_bytes is None for card in cards)
    assert any(card.entry.size_bytes is not None for card in cards)
    for card in cards:
        card.show()
    qapp.processEvents()
    try:
        for part in ("title_label", "tier_badge", "delivery_label"):
            tops = {
                kind: _top(card, getattr(card, part))
                for kind, card in zip(kinds, cards, strict=True)
            }
            assert len(set(tops.values())) == 1, (part, tops)
    finally:
        for card in cards:
            card.close()
            card.deleteLater()
        qapp.processEvents()


def test_cards_describe_themselves_to_screen_readers(qapp):
    sized = StoreCard(_entry("direct-pgr"))
    unsized = StoreCard(_entry("stove-czn"))
    description = sized.accessibleDescription()
    assert sized.entry.tier in description
    assert sized.entry.delivery_label in description
    assert "GiB" in description or "GB" in description
    assert "GiB" not in unsized.accessibleDescription()
    assert unsized.entry.tier in unsized.accessibleDescription()


def test_opening_the_store_focuses_the_search_box(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.show()
        qapp.processEvents()
        window.show_store()
        qapp.processEvents()
        assert window.focusWidget() is window.store.grid.search
    finally:
        _close(window, qapp)


def test_back_from_a_detail_returns_focus_to_the_card_that_was_opened(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.show()
        window.show_store()
        qapp.processEvents()
        grid = window.store.grid
        grid.entry_activated.emit("direct-pgr")
        qapp.processEvents()
        window.store.detail.back_button.click()
        qapp.processEvents()
        assert window.focusWidget() is grid._cards["direct-pgr"]
        # If a search hid that card meanwhile, focus falls back to the search box.
        grid.entry_activated.emit("direct-pgr")
        window.store.detail.back_button.click()
        grid.search.setText("neverness")
        qapp.processEvents()
        window.store.show_detail("direct-nte")
        window.store.detail.back_button.click()
        qapp.processEvents()
        assert window.focusWidget() is grid._cards["direct-nte"]
    finally:
        _close(window, qapp)


def test_back_falls_back_to_the_search_box_when_the_card_is_gone(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.show()
        window.show_store()
        qapp.processEvents()
        grid = window.store.grid
        window.store.show_detail("direct-pgr")
        grid.search.setText("zzz-no-such-game")
        window.store.detail.back_button.click()
        qapp.processEvents()
        assert window.focusWidget() is grid.search
    finally:
        _close(window, qapp)


def test_the_grid_fits_the_minimum_window_size(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.resize(880, 560)
        window.show_store()
        window.show()
        qapp.processEvents()
        grid = window.store.grid
        assert grid.scroll.horizontalScrollBar().maximum() == 0
        area = grid.rect()
        for widget in (grid.search, grid.tier_filter, grid.own_game_button):
            assert area.contains(widget.mapTo(grid, widget.rect().topLeft()))
            assert area.contains(widget.mapTo(grid, widget.rect().bottomRight()))
    finally:
        _close(window, qapp)


@pytest.mark.parametrize("kind", ["launcher-nikke", "fgo-lepton", "direct-pgr"])
def test_the_install_button_stays_reachable_at_the_minimum_window_size(tmp_path, qapp, kind):
    window = _window(tmp_path)
    try:
        window.resize(880, 560)
        window.show_store()
        window.show()
        qapp.processEvents()
        _entry(kind)
        window.store.show_detail(kind)
        qapp.processEvents()
        detail = window.store.detail
        button = detail.install_button
        # The primary action must be visible in the viewport without scrolling.
        bottom = button.mapTo(window.store, button.rect().bottomLeft()).y()
        assert bottom <= window.store.height(), (kind, bottom, window.store.height())
        assert button.isVisible()
    finally:
        _close(window, qapp)


def test_launcher_games_do_not_show_the_old_100_gib_placeholder(qapp):
    from nadare.core import backends

    sizes = {e.kind: e.size_bytes for e in entries() if e.kind.startswith("launcher-")}
    assert set(sizes) == {"launcher-nikke", "launcher-nte", "launcher-gfl2-haoplay"}
    assert all(size != 100 * 1024**3 for size in sizes.values())
    # NIKKE: measured on a real install (27 GiB including the Wine prefix).
    assert sizes["launcher-nikke"] == 27 * 1024**3
    # The launcher installs the same game as the direct kind.
    assert sizes["launcher-nte"] == backends.install_size_hint("direct-nte")
    # GFL2: a tiny launcher, then the game downloads about 60 GB inside itself.
    assert 55 * 10**9 <= sizes["launcher-gfl2-haoplay"] <= 65 * 10**9
