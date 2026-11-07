# SPDX-License-Identifier: GPL-3.0-or-later
"""Store cards share the row's width instead of leaving a wide empty gutter (#857)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.gui import store
from nadare.gui.store import StorePage

GAP = 18


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


@pytest.fixture
def page(qapp):
    widget = StorePage()
    widget.refresh()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _fit(page, qapp, width, height=900):
    page.resize(width, height)
    page.show()
    qapp.processEvents()
    qapp.processEvents()
    return list(page._cards.values())


def _viewport(page) -> int:
    return page.scroll.viewport().width() - 4  # the layout's own margins


@pytest.mark.parametrize("width", [800, 1050, 1280, 1600, 1920])
def test_cards_fill_the_row_without_a_wide_gutter(page, qapp, width):
    cards = _fit(page, qapp, width)
    assert len(cards) >= 4
    sizes = {card.width() for card in cards}
    assert len(sizes) == 1
    (card_w,) = sizes
    assert store.CARD_MIN_WIDTH <= card_w <= store.CARD_MAX_WIDTH
    avail = _viewport(page)
    per_row = max(1, (avail + GAP) // (card_w + GAP))
    used = per_row * card_w + (per_row - 1) * GAP
    assert 0 <= avail - used <= per_row + GAP, (width, card_w, per_row, avail)


def test_1050_pixels_shows_three_columns_not_two(page, qapp):
    cards = _fit(page, qapp, 1050)
    top = min(card.y() for card in cards)
    assert sum(1 for card in cards if card.y() == top) >= 3


def test_a_window_narrower_than_a_card_scrolls_instead_of_squeezing(page, qapp):
    cards = _fit(page, qapp, 300)
    assert {card.width() for card in cards} == {store.CARD_MIN_WIDTH}


def test_the_art_follows_the_card_and_keeps_its_height(page, qapp):
    for card in _fit(page, qapp, 1280):
        assert card.art.width() == card.width() - 2
        assert card.art.height() == store.CARD_ART_H
        assert card.height() == store.CARD_HEIGHT


def test_a_fetched_cover_is_redrawn_for_the_new_width(page, qapp):
    from PyQt6.QtGui import QColor, QPixmap

    card = _fit(page, qapp, 1050)[0]
    cover = QPixmap(640, 360)
    cover.fill(QColor("red"))
    card.set_art(cover)
    before = card.art.pixmap().width()
    _fit(page, qapp, 1300)  # still three columns, so the cards grow
    assert card.art.pixmap().width() > before
    assert card.art.pixmap().width() == card.width() - 2


def test_cards_keep_keyboard_focus_and_an_accessible_name(page, qapp):
    for card in _fit(page, qapp, 1050):
        assert card.focusPolicy() != store.Qt.FocusPolicy.NoFocus
        assert card.accessibleName()
