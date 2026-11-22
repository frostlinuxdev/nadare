# SPDX-License-Identifier: GPL-3.0-or-later
"""Store coming-soon cards, using local art and an injected feed."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PyQt6.QtGui import QColor, QDesktopServices, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from nadare.core import artwork, upcoming
from nadare.core.storecatalog import StoreGame
from nadare.gui import store
from nadare.gui.store import StorePage

#: The real loader, for the one test that drives the whole missing-file path (the page
#: fixture replaces ``upcoming.load`` so no test ever reaches the network).
_REAL_LOAD = upcoming.load


def _wait(condition, timeout: float = 5.0) -> bool:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def page(monkeypatch, tmp_path):
    sample = json.loads((Path(__file__).parent / "fixtures/upcoming.json").read_text())
    monkeypatch.setattr(upcoming, "load", lambda: upcoming.parse(sample))
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)
    art = tmp_path / "fixture-art.png"
    image = QPixmap(400, 220)
    image.fill(QColor("#446677"))
    assert image.save(str(art))
    downloads = []

    def download(url, slug, kind, allowed_hosts):
        downloads.append((url, allowed_hosts))
        return art

    monkeypatch.setattr(artwork, "download", download)
    widget = StorePage()
    widget.resize(1200, 800)
    widget.show()
    assert _wait(lambda: len(widget._upcoming_cards) == 3)
    assert _wait(lambda: widget._upcoming_cards[0]._art_source is not None)
    widget.wait_for_images()
    QApplication.instance().processEvents()
    yield widget, downloads
    widget.close()
    widget.deleteLater()
    QApplication.instance().processEvents()


def test_plain_cards_with_art_placeholder_and_official_link(page, monkeypatch):
    widget, downloads = page
    assert not widget.upcoming_row.isHidden()
    assert [card.item.title for card in widget._upcoming_cards] == [
        "Silver Palace",
        "Star Garden",
        "Moon Voyage",
    ]
    labels = [label.text() for label in widget.upcoming_row.findChildren(QLabel)]
    for text in ("Coming soon", "Silver Palace", "Silver Studio", "Q1 2027", "Moon Studio"):
        assert text in labels
    assert downloads == [("https://cdn2.unrealengine.com/sample.jpg", upcoming.art_hosts())]
    assert widget._upcoming_cards[0]._art_source is not None
    assert widget._upcoming_cards[1]._art_source is None
    assert not widget._upcoming_cards[1].art.pixmap().isNull()

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    button = widget._upcoming_cards[0].findChild(QPushButton)
    assert button is not None and button.text() == "Official page"
    button.click()
    assert opened == ["https://silver.example/"]
    assert widget._upcoming_cards[2].findChild(QPushButton).text() == "Official page"

    # A screenshot is an explicit opt-in, never a routine test write outside tmp_path.
    if destination := os.environ.get("NADARE_UPCOMING_SCREENSHOT"):
        from nadare.gui import theme

        row = widget.upcoming_row
        previous_sheet, previous_name = row.styleSheet(), row.objectName()
        row.setObjectName("UpcomingShot")
        row.setStyleSheet(
            theme.stylesheet() + f"QWidget#UpcomingShot {{ background: {theme.BG}; }}"
        )
        row.ensurePolished()
        QApplication.instance().processEvents()
        shot = QPixmap(row.size())
        shot.fill(QColor(theme.BG))
        row.render(shot)
        row.setStyleSheet(previous_sheet)
        row.setObjectName(previous_name)
        assert shot.save(destination)


def test_art_is_downloaded_once_across_search_rebuilds(page):
    widget, downloads = page
    assert len(downloads) == 1
    widget.search.setText("silver")
    widget.wait_for_images()
    widget.search.setText("")
    widget.wait_for_images()
    QApplication.instance().processEvents()
    assert len(downloads) == 1
    assert widget._upcoming_cards[0]._art_source is not None


def test_catalog_title_is_hidden_even_with_a_search(page, monkeypatch):
    widget, _ = page
    real = store.store_games
    game = real()[0]
    monkeypatch.setattr(
        store,
        "store_games",
        lambda **kw: [*real(**kw), StoreGame(" silver PALACE ", game.entries)],
    )
    widget.search.setText("does not match any real game")
    QApplication.instance().processEvents()
    titles = [card.item.title for card in widget._upcoming_cards]
    assert "Silver Palace" not in titles
    assert titles == ["Star Garden", "Moon Voyage"]
    assert not widget.upcoming_row.isHidden()
    assert widget.results.currentWidget() is widget.scroll


@pytest.mark.parametrize("document", [None, {}, {"upcoming": []}, {"upcoming": [{"title": 4}]}])
def test_empty_or_missing_feed_means_no_row(page, document):
    widget, _ = page
    widget._on_upcoming_loaded(upcoming.parse(document))
    assert widget.upcoming_row.isHidden()


def test_a_missing_file_leaves_no_row_and_reports_nothing(page, monkeypatch):
    """The whole path: an index with no ``catalog/upcoming.json`` is quiet, not an error."""
    widget, _ = page
    monkeypatch.setattr(
        upcoming, "feed_url", lambda: "https://index.example/catalog/upcoming.json"
    )

    def missing(url, timeout, max_bytes):
        raise FileNotFoundError(url)

    monkeypatch.setattr(upcoming.indexfetch, "open_bytes", missing)
    assert _REAL_LOAD() == []
    widget._on_upcoming_loaded(_REAL_LOAD())
    assert widget._upcoming_cards == []
    assert widget.upcoming_row.isHidden()


def test_tab_from_the_last_card_reaches_the_official_pages(page):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    widget, _ = page
    last = widget._cards[widget.visible_kinds()[-1]]
    last.setFocus()
    QApplication.instance().processEvents()
    assert QApplication.focusWidget() is last
    QTest.keyClick(last, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is widget._upcoming_cards[0].button
    QTest.keyClick(widget._upcoming_cards[0].button, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is widget._upcoming_cards[1].button


def test_a_card_without_a_link_has_no_button(page):
    widget, _ = page
    document = {"upcoming": [{"title": "No Link", "publisher": "P", "when": "2027"}]}
    widget._on_upcoming_loaded(upcoming.parse(document))
    [card] = widget._upcoming_cards
    assert card.findChildren(QPushButton) == []
    assert not widget.upcoming_row.isHidden()


def test_safe_opener_rejects_non_web_links(monkeypatch):
    from nadare.gui.links import open_external

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    for link in ("file:///tmp/x", "javascript:alert(1)", "ftp://example.com/x", "https:", ""):
        open_external(link)
    assert not opened
    open_external("https://silver.example/")
    assert opened == ["https://silver.example/"]


@pytest.mark.parametrize("operation", ["feed_failure", "art_success"])
def test_background_completion_after_page_deleted_is_safe(monkeypatch, qtbot, tmp_path, operation):
    from PyQt6 import sip
    from PyQt6.QtCore import QThreadPool

    from nadare.gui.workers import CallTask

    queued = []
    monkeypatch.setattr(QThreadPool, "start", lambda self, task: queued.append(task))

    def offline():
        raise OSError("offline")

    monkeypatch.setattr(upcoming, "load", offline)
    monkeypatch.setattr(artwork, "download", lambda *a, **k: tmp_path / "local art.png")
    widget = StorePage()
    widget.show()
    if operation == "art_success":
        widget._on_upcoming_loaded([
            upcoming.Upcoming("Future", "Studio", "2027", art="https://cdn2.unrealengine.com/a.png")
        ])
    task = [task for task in queued if isinstance(task, CallTask)][-1]
    sip.delete(widget)
    with qtbot.captureExceptions() as errors:
        task.run()
        QApplication.instance().processEvents()
    assert errors == []
