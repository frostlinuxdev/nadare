# SPDX-License-Identifier: GPL-3.0-or-later
"""#975 item 2: a silent news refresh redraws a card whose date or category changed.

The page keeps its cards when a refresh returns the same items (no flash). The key left out the
date and category the cards show, so a re-dated or re-categorised article kept its old values.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.news import NewsResult
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("g", {"backend": "direct-pgr", "title": "PGR"})
    yield page
    page.deleteLater()
    qapp.processEvents()


def _item(**changes) -> dict:
    item = {
        "id": "1",
        "title": "Patch notes",
        "url": "https://example.test/1",
        "category": "Notice",
        "published_at": "2026-11-01T10:00:00Z",
    }
    item.update(changes)
    return item


def test_the_same_items_keep_their_cards(view):
    view.set_news(NewsResult(items=[_item()], available=True))
    first = list(view.news_rows)
    view.set_news(NewsResult(items=[_item()], available=True))
    assert view.news_rows == first


@pytest.mark.parametrize(
    "changes", [{"published_at": "2026-11-08T10:00:00Z"}, {"category": "Event"}]
)
def test_a_changed_date_or_category_redraws(view, changes):
    view.set_news(NewsResult(items=[_item()], available=True))
    first = list(view.news_rows)
    view.set_news(NewsResult(items=[_item(**changes)], available=True))
    assert view.news_rows != first
