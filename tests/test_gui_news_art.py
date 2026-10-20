# SPDX-License-Identifier: GPL-3.0-or-later
"""News cards without their own image use the game's art; refreshes don't flash."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.news import NewsResult


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _png(path, color: str) -> str:
    image = QImage(64, 36, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))
    return str(path)


def _item(index: int, image_path: str | None = None) -> dict:
    return {
        "id": str(index),
        "title": f"News {index}",
        "summary": "",
        "category": "News",
        "published_at": 1_700_000_000_000,
        "image": None,
        "image_path": image_path,
        "url": f"https://example.invalid/news/{index}",
    }


def _center_color(card) -> str:
    source = card.thumb._source
    assert source is not None, "thumbnail fell back to the gradient placeholder"
    image = source.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2).name()


def _view(monkeypatch, art: list[str]):
    from nadare.gui import game_view as game_view_mod
    from nadare.gui.game_view import GameView

    monkeypatch.setattr(game_view_mod, "profile_art_candidates", lambda profile: list(art))
    view = GameView()
    view.set_profile("sample", {"backend": "fgo"})
    return view


def test_imageless_news_card_uses_the_game_art(tmp_path, qapp, monkeypatch):
    art = _png(tmp_path / "cover.png", "#ff0000")
    view = _view(monkeypatch, [art])
    try:
        view.set_news(NewsResult(items=[_item(1, None)]))
        view.wait_for_images()
        qapp.processEvents()
        assert _center_color(view.news_rows[0]) == "#ff0000"
    finally:
        view.deleteLater()


def test_missing_news_image_file_falls_back_to_the_game_art(tmp_path, qapp, monkeypatch):
    art = _png(tmp_path / "cover.png", "#ff0000")
    view = _view(monkeypatch, [art])
    try:
        view.set_news(NewsResult(items=[_item(1, str(tmp_path / "gone.png"))]))
        view.wait_for_images()
        qapp.processEvents()
        assert _center_color(view.news_rows[0]) == "#ff0000"
    finally:
        view.deleteLater()


def test_news_card_with_its_own_image_keeps_it(tmp_path, qapp, monkeypatch):
    art = _png(tmp_path / "cover.png", "#ff0000")
    own = _png(tmp_path / "own.png", "#0000ff")
    view = _view(monkeypatch, [art])
    try:
        view.set_news(NewsResult(items=[_item(1, own)]))
        view.wait_for_images()
        qapp.processEvents()
        assert _center_color(view.news_rows[0]) == "#0000ff"
    finally:
        view.deleteLater()


def test_unchanged_news_refresh_keeps_the_same_cards(tmp_path, qapp, monkeypatch):
    own = _png(tmp_path / "own.png", "#0000ff")
    view = _view(monkeypatch, [])
    try:
        view.set_news(NewsResult(items=[_item(1, own), _item(2, None)]))
        first = list(view.news_rows)
        view.set_news(NewsResult(items=[_item(1, own), _item(2, None)]))
        assert view.news_rows == first
        assert all(a is b for a, b in zip(view.news_rows, first, strict=True))
    finally:
        view.deleteLater()


def test_changed_news_refresh_rebuilds_the_cards(tmp_path, qapp, monkeypatch):
    view = _view(monkeypatch, [])
    try:
        view.set_news(NewsResult(items=[_item(1), _item(2)]))
        first = list(view.news_rows)
        view.set_news(NewsResult(items=[_item(3), _item(1), _item(2)]))
        assert len(view.news_rows) == 3
        assert view.news_rows[0] is not first[0]
        assert [card._title for card in view.news_rows] == ["News 3", "News 1", "News 2"]
    finally:
        view.deleteLater()


def test_switching_profile_with_same_news_ids_rebuilds(tmp_path, qapp, monkeypatch):
    """Two games can share item ids; a new profile must never reuse old cards."""
    view = _view(monkeypatch, [])
    try:
        view.set_news(NewsResult(items=[_item(1)]))
        first = list(view.news_rows)
        view.set_profile("other", {"backend": "fgo"})
        view.set_news(NewsResult(items=[_item(1)]))
        assert view.news_rows[0] is not first[0]
    finally:
        view.deleteLater()


def test_image_cache_holds_several_games_worth_of_art(qapp):
    from nadare.gui.images import ImageLoader

    assert ImageLoader()._capacity >= 96
