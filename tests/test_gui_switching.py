# SPDX-License-Identifier: GPL-3.0-or-later
"""Game-switch smoothness: snapshot replay, silent refresh and off-thread images."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.news import NewsResult
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _fresh(status=None, news=None, media=None):
    now = time.monotonic()
    return {
        "status": status,
        "status_at": now,
        "news": news,
        "news_at": now if news is not None else None,
        "media": media,
        "media_at": now if media is not None else None,
    }


def _two_profile_window(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="arknights-yostar", title="Alpha")
    config.add_profile("beta", backend="arknights-yostar", title="Beta")
    window = MainWindow(config)
    started: list[str] = []
    window.controller.start = lambda profile, op, payload=None: started.append(op) or True
    window._started = started  # type: ignore[attr-defined]
    return window


def test_switch_replays_snapshots_without_busy_or_cancel(tmp_path, qapp):
    window = _two_profile_window(tmp_path, qapp)
    window._snapshots["alpha"] = _fresh(Status("installed", version="1.0.0"))
    window._snapshots["beta"] = _fresh(Status("not_installed", version="2.0.0"))
    busy_calls: list[str] = []
    window.game.set_busy = busy_calls.append  # type: ignore[method-assign]
    texts: list[str] = []
    real_set_text = window.game.primary.setText
    window.game.primary.setText = lambda text: (texts.append(text), real_set_text(text))[1]
    try:
        window.open_game("alpha")
        assert window.game.version_label.text() == "v1.0.0"
        window.open_game("beta")
        assert window.game.version_label.text() == "v2.0.0"
        window.open_game("alpha")
        assert window.game.version_label.text() == "v1.0.0"

        assert "CANCEL" not in texts, texts
        assert not any(text.startswith("CHECKING") for text in texts), texts
        assert busy_calls == [], "background status must not put the page into busy"
        # The page is actionable immediately from the snapshot.
        assert window.game.primary.text() == "LAUNCH"
        assert window.game.primary.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_silent_status_never_marks_the_profile_busy(tmp_path, qapp):
    window = _two_profile_window(tmp_path, qapp)
    busy_calls: list[str] = []
    window.game.set_busy = busy_calls.append  # type: ignore[method-assign]
    try:
        window._on_busy("alpha", "status", True)
        assert "alpha" not in window._busy
        assert busy_calls == []
        # A real operation still does.
        window._on_busy("alpha", "install", True)
        assert window._busy["alpha"] == "install"
        assert busy_calls == ["install"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_fresh_snapshot_skips_news_and_media_on_switch(tmp_path, qapp):
    window = _two_profile_window(tmp_path, qapp)
    news = NewsResult(items=[{"id": "1", "title": "t", "url": "https://x/1"}])
    window._snapshots["alpha"] = _fresh(
        Status("installed"), news=news, media={"kind": "media", "hero": None, "banners": []}
    )
    try:
        window._started.clear()
        window.open_game("alpha")
        assert window._started == ["status"]
        # A successful status for a fresh snapshot must not refetch either feed.
        window._on_succeeded("alpha", "status", 0)
        assert window._started == ["status"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_stale_snapshot_refreshes_news_after_status(tmp_path, qapp):
    window = _two_profile_window(tmp_path, qapp)
    window._snapshots["alpha"] = _fresh(Status("installed"))  # no news/media yet
    try:
        window._started.clear()
        window._on_succeeded("alpha", "status", 0)
        assert window._started == ["news"]
        window._on_succeeded("alpha", "news", 0)
        assert window._started == ["news", "media"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_image_loader_decodes_off_thread_and_hits_lru(qapp, tmp_path):
    from PyQt6.QtCore import QThread
    from PyQt6.QtGui import QImage

    from nadare.gui.images import ImageLoader

    path = tmp_path / "big.png"
    image = QImage(800, 600, QImage.Format.Format_ARGB32)
    image.fill(0xFF3366FF)
    assert image.save(str(path))

    threads: list[bool] = []

    def fake_decode(source, width, height, *, crop=True):
        threads.append(QThread.currentThread() is qapp.thread())
        return QImage(str(source)).scaled(width, height)

    loader = ImageLoader(decode=fake_decode)
    got: list = []
    loader.request([str(path)], 64, 48, got.append)
    loader.wait(3000)
    qapp.processEvents()

    assert got and got[0] is not None and not got[0].isNull()
    assert threads == [False], "decode must run off the GUI thread"
    assert loader.pending() == 0

    loader.request([str(path)], 64, 48, got.append)
    assert len(threads) == 1, "a repeat request must be served from the LRU"


def test_game_view_decodes_profile_art_off_thread(qapp, tmp_path):
    from PyQt6.QtCore import QThread
    from PyQt6.QtGui import QImage

    from nadare.gui import images
    from nadare.gui.game_view import GameView

    cover = tmp_path / "cover.png"
    image = QImage(640, 960, QImage.Format.Format_ARGB32)
    image.fill(0xFF2288FF)
    assert image.save(str(cover))

    threads: list[bool] = []
    real_decode = images.decode_scaled

    def spy(source, width, height, *, crop=True):
        threads.append(QThread.currentThread() is qapp.thread())
        return real_decode(source, width, height, crop=crop)

    view = GameView()
    view.images._decode = spy  # type: ignore[attr-defined]
    try:
        view.set_profile("sample", {"title": "Sample", "cover": str(cover)})
        view.wait_for_images()
        qapp.processEvents()
        assert threads == [False]
        assert view.art_label.pixmap() is not None
        assert not view.art_label.pixmap().isNull()

        # Switching back to the same profile must hit the scaled-pixmap LRU.
        view.set_profile("sample", {"title": "Sample", "cover": str(cover)})
        view.wait_for_images()
        qapp.processEvents()
        assert len(threads) == 1
    finally:
        view.deleteLater()
        qapp.processEvents()
