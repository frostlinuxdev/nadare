# SPDX-License-Identifier: GPL-3.0-or-later
"""Library and favourite cards match a Continue playing tile: size, and the name on the art.

Asked by the user on 2026-11-21.
"""

from datetime import UTC, datetime, timedelta

import pytest
from PyQt6.QtWidgets import QApplication, QScrollArea

from nadare.gui import library


def _profiles():
    now = datetime.now(UTC)
    out = {str(i): {"title": f"Game {i}"} for i in range(10)}
    for i in range(3):
        out[str(i)]["last_played"] = (now - timedelta(hours=i + 1)).isoformat()
    out["5"]["favourite"] = True
    return out


@pytest.fixture
def page(monkeypatch):
    app = QApplication.instance() or QApplication([])
    page = library.LibraryPage()
    requests = []
    monkeypatch.setattr(page._images, "request", lambda *a, **k: requests.append(a))
    page.requests = requests
    page.refresh(_profiles())
    page.add = library.AddGameCard()
    page._flow.addWidget(page.add)
    page.show()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _settle(page, width):
    app = QApplication.instance()
    page.resize(width, 1000)
    for _ in range(5):
        app.processEvents()


@pytest.mark.parametrize("width", [700, 1280, 2560])
def test_every_card_is_the_size_of_a_continue_playing_tile(page, width):
    _settle(page, width)
    tile = page.recent_tiles[0].size()
    assert tile.width() * 9 // 16 - 1 <= tile.height() <= tile.width() * 9 // 16 + 1
    cards = [*page._cards.values(), *page._favourite_cards.values(), page.add]
    assert {c.size() for c in cards} == {tile}
    assert page.findChild(QScrollArea).horizontalScrollBar().maximum() == 0


def test_the_name_sits_on_the_art_in_the_tile_style(page):
    _settle(page, 1280)
    card = next(iter(page._cards.values()))
    tile = page.recent_tiles[0]
    assert card.title_label.objectName() == tile.name.objectName() == "TileTitle"
    assert card.title_label.geometry() == tile.name.geometry()
    assert card.art.geometry() == card.rect()
    assert not card.meta_label.isVisible()
    assert card.play_button.geometry() == tile.play_button.geometry()


def test_a_favourite_keeps_its_heart_beside_the_name(page):
    _settle(page, 1280)
    card = page._favourite_cards["5"]
    assert card.heart.geometry().right() < card.title_label.geometry().left()
    assert card.heart.y() == card.title_label.y()


def test_a_state_shows_as_the_tile_s_top_badge(page):
    _settle(page, 1280)
    card = page._cards["7"]
    card.set_state("installing")
    assert card.badge.isVisibleTo(card) and card.badge.y() < card.height() // 2
    assert not card.new_pill.isVisibleTo(card)


def test_cards_ask_for_wide_art_like_the_tiles(page, monkeypatch):
    monkeypatch.setattr(library, "recent_art_candidates", lambda p: ["WIDE:" + p["title"]])
    monkeypatch.setattr(library, "profile_art_candidates", lambda p: ["PORTRAIT:" + p["title"]])
    page.requests.clear()
    page.refresh({**_profiles(), "new": {"title": "Fresh"}})
    asked = [r[0] for r in page.requests]
    assert ["WIDE:Fresh"] in asked
    assert not any(a and str(a[0]).startswith("PORTRAIT:") for a in asked)


def _bright(width, height):
    from PyQt6.QtGui import QColor, QPixmap

    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("white"))
    return pixmap


@pytest.mark.parametrize("kind", ["card", "tile"])
def test_the_state_badge_is_solid_on_bright_art(kind):
    """A NOT INSTALLED badge stays readable on white art: an opaque pill sized to its text."""
    from nadare.gui import theme

    app = QApplication.instance() or QApplication([])
    if kind == "card":
        widget = library.GameCard("a", "Alpha")
    else:
        widget = library.RecentTile("a", "Alpha", "")
    widget.setStyleSheet(theme.stylesheet())
    widget.set_art(_bright(widget.width(), widget.height()))
    widget.set_state("not_installed")
    app.processEvents()
    badge = widget.badge
    assert badge.objectName() == "TileBadge" and badge.text() == "NOT INSTALLED"
    assert badge.width() < widget.width() // 2  # a pill, not a full-width line
    image = widget.grab().toImage()
    padding = image.pixelColor(badge.x() + 4, badge.geometry().center().y())
    beside = image.pixelColor(badge.geometry().right() + 6, badge.geometry().center().y())
    assert beside.lightness() > 200  # the art here is white
    assert padding.lightness() < 80  # and does not show through the pill
    widget.deleteLater()
    app.processEvents()
