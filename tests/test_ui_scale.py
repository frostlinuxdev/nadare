# SPDX-License-Identifier: GPL-3.0-or-later
"""Library design dimensions follow the app UI scale, including overlays and strips."""

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

from nadare.gui import library, theme
from nadare.gui.library import LibraryPage


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_library_controls_and_tiles_scale(monkeypatch, scale):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(theme, "UI_SCALE", scale)
    page = LibraryPage()
    try:
        page.refresh({"a": {"favourite": True, "last_played": "2026-11-17"}})
        available = round(1016 * scale)
        page._flow.setGeometry(QRect(0, 0, available, 1000))
        page._fav_flow.setGeometry(QRect(0, 0, available, 1000))
        page._recent_flow.setGeometry(QRect(0, 0, available, 1000))
        card = page._cards["a"]
        fav = page._favourite_cards["a"]
        assert page.search.height() == theme.scaled(44)
        assert page.add_button.height() == theme.scaled(44)
        assert page.layout().contentsMargins().left() == theme.scaled(28)
        assert fav.width() == card.width() == library.tile_width_for(available)
        assert fav.art.height() == card.art.height() == round(card.width() * 9 / 16)
        assert card.play_button.width() == theme.scaled(32)
        assert card.new_pill.height() == theme.scaled(18)
        tile = page.recent_tiles[0]
        assert tile.size() == card.size()
        assert tile.height() == round(tile.width() * 9 / 16)
    finally:
        page.wait_for_images()
        page.deleteLater()
        app.processEvents()
