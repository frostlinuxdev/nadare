# SPDX-License-Identifier: GPL-3.0-or-later
"""News cards and banners can be reached and opened from the keyboard (#901)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.game_view import NewsCard, _BannerCard

URL = "https://news.example/article"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _news(url=URL):
    return NewsCard({"title": "Version 3.7 is out", "url": url, "category": "News"})


def _banner(url=URL):
    return _BannerCard("/nonexistent.png", url)


@pytest.mark.parametrize("make", [_news, _banner])
def test_a_card_with_a_link_can_take_focus_and_has_a_name(qapp, make):
    card = make()
    assert card.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert card.accessibleName()


def test_the_news_name_is_its_title(qapp):
    assert "Version 3.7 is out" in _news().accessibleName()


@pytest.mark.parametrize("make", [_news, _banner])
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space])
def test_enter_and_space_open_the_link(qapp, make, key):
    card = make()
    card.show()
    opened: list[str] = []
    card.clicked.connect(opened.append)
    card.setFocus()
    QTest.keyClick(card, key)
    assert opened == [URL]
    card.hide()


@pytest.mark.parametrize("make", [_news, _banner])
def test_other_keys_do_nothing(qapp, make):
    card = make()
    card.show()
    opened: list[str] = []
    card.clicked.connect(opened.append)
    QTest.keyClick(card, Qt.Key.Key_A)
    assert opened == []
    card.hide()


@pytest.mark.parametrize("make", [_news, _banner])
def test_a_card_without_a_link_is_not_a_tab_stop_and_opens_nothing(qapp, make):
    card = make(url="")
    assert card.focusPolicy() == Qt.FocusPolicy.NoFocus
    opened: list[str] = []
    card.clicked.connect(opened.append)
    card.show()
    QTest.keyClick(card, Qt.Key.Key_Return)
    assert opened == []
    card.hide()


@pytest.mark.parametrize("make", [_news, _banner])
def test_a_focused_card_shows_a_focus_ring(qapp, make):
    assert ":focus" in make().styleSheet()
