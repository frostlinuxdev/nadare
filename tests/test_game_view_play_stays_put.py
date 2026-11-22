# SPDX-License-Identifier: GPL-3.0-or-later
"""The LAUNCH/Play button sits in the same place whether or not the game has news (user 2026-11-21).

Without news the hero header used to take all the spare height and push the button to the bottom
of the page.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.news import NewsResult
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _news(count: int) -> NewsResult:
    items = [
        {
            "id": str(i),
            "title": f"Patch {i}",
            "summary": "notes",
            "category": "Updates",
            "published_at": 1_700_000_000_000,
            "image": None,
            "url": f"https://example.invalid/{i}",
        }
        for i in range(count)
    ]
    return NewsResult(items=items)


def _primary_y(qapp, size, news) -> int:
    view = GameView()
    try:
        view.resize(*size)
        view.set_profile("sample", {"backend": "stove-czn", "title": "Sample"})
        if news is None:
            view._set_news_visible(False)
        else:
            view.set_news(news)
        view.show()
        for _ in range(5):
            qapp.processEvents()
        assert view.news_frame.isHidden() is (news is None)
        return view.primary.mapTo(view, QPoint(0, 0)).y()
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("size", [(1280, 720), (1400, 900), (1920, 1080), (2560, 1440)])
def test_play_button_does_not_move_when_news_is_missing(qapp, size):
    with_news = _primary_y(qapp, size, _news(6))
    without = _primary_y(qapp, size, None)
    empty = _primary_y(qapp, size, _news(0))
    assert without == with_news == empty
