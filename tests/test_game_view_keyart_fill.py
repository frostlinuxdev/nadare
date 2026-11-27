# SPDX-License-Identifier: GPL-3.0-or-later
"""Key art fills an empty page without moving the fixed-height hero (#1742)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

from nadare.core.backends import Status
from nadare.core.news import NewsResult
from nadare.gui import theme
from nadare.gui.game_view import GameView, HeroCard
from nadare.gui.images import ImageLoader


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def artwork():
    art = QPixmap(1600, 900)
    art.fill(QColor("#eedda0"))
    painter = QPainter(art)
    for x in range(0, 1600, 100):
        painter.fillRect(x, 0, 50, 900, QColor("#70b9db"))
    painter.end()
    return art


@contextmanager
def page(qapp, monkeypatch, news=False):
    def request(self, candidates, width, height, callback, **kwargs):
        callback(artwork() if candidates == ["fixture-keyart"] else None)

    monkeypatch.setattr(ImageLoader, "request", request)
    view = GameView()
    view.setStyleSheet(theme.stylesheet())
    view.resize(1280, 900)
    view.set_profile("sample", {"backend": "wine", "title": "Sample game"})
    view.set_status(Status("installed"))
    view.set_media("fixture-keyart")
    if news:
        view.set_news(
            NewsResult(
                items=[
                    {
                        "id": "1",
                        "title": "Update",
                        "summary": "Publisher news",
                        "published_at": 1_700_000_000_000,
                        "image": None,
                        "url": "https://example.invalid/news",
                    }
                ]
            )
        )
    view.show()
    for _ in range(5):
        qapp.processEvents()
    try:
        yield view
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def launch_rect(view):
    return (
        view.primary.mapTo(view, QPoint()).x(),
        view.primary.mapTo(view, QPoint()).y(),
        view.primary.width(),
        view.primary.height(),
    )


def tail_pixels(view):
    content = view.scroll.widget()
    image = content.grab().toImage()
    ratio = image.devicePixelRatio()
    hero = view.hero.geometry()
    return [
        image.pixelColor(round(x * ratio), round(y * ratio)).rgba()
        for x in (hero.center().x(), hero.right() - 20)
        for y in (hero.bottom() + 30, content.height() - 50)
    ]


def test_no_news_art_reaches_below_the_header(qapp, monkeypatch):
    with page(qapp, monkeypatch) as view:
        assert view.news_frame.isHidden()
        painted = tail_pixels(view)
        before = launch_rect(view)
        view.hero.set_source(None)
        qapp.processEvents()
        plain = tail_pixels(view)
        assert all(a != b for a, b in zip(painted, plain, strict=True)), (
            "art stops at the hero header"
        )
        view.set_media("fixture-keyart")
        view.set_news(NewsResult(items=[{"id": "1", "title": "News"}]))
        qapp.processEvents()
        assert launch_rect(view) == before


def test_news_keeps_the_original_hero_and_page_pixels(qapp, monkeypatch):
    with page(qapp, monkeypatch, news=True) as view:
        before = launch_rect(view)
        content = view.scroll.widget()
        plain_page = QWidget()
        plain_page.setObjectName("Page")
        plain_page.setStyleSheet(theme.stylesheet())
        plain_page.resize(content.size())
        plain_page.show()
        qapp.processEvents()
        actual_page = content.grab().toImage()
        expected_page = plain_page.grab().toImage()
        ratio = actual_page.devicePixelRatio()
        for y in (0, content.height() // 2, content.height() - 20):
            assert actual_page.pixelColor(0, round(y * ratio)) == expected_page.pixelColor(
                0, round(y * ratio)
            )
        plain_page.close()
        plain_page.deleteLater()
        # The old hero painter is still the reference when news occupies the page.
        reference = HeroCard()
        reference.setStyleSheet(theme.stylesheet())
        reference.resize(view.hero.size())
        reference.set_source(artwork())
        reference.show()
        qapp.processEvents()
        background = QPixmap(view.hero.size() * view.devicePixelRatioF())
        background.setDevicePixelRatio(view.devicePixelRatioF())
        view.hero.render(background, flags=QWidget.RenderFlag.DrawWindowBackground)
        actual = background.toImage()
        expected = reference.grab().toImage()
        ratio = actual.devicePixelRatio()
        for x in (view.hero.width() - 30, view.hero.width() - 100):
            for y in (30, view.hero.height() // 2, view.hero.height() - 30):
                assert actual.pixelColor(round(x * ratio), round(y * ratio)) == expected.pixelColor(
                    round(x * ratio), round(y * ratio)
                )
        reference.close()
        reference.deleteLater()
        painted = tail_pixels(view)
        view.hero.set_source(None)
        qapp.processEvents()
        assert tail_pixels(view) == painted
        assert launch_rect(view) == before
