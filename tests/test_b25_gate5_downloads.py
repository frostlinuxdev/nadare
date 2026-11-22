# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate 5: a late cover response must still belong to its Downloads row."""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QColor, QPixmap

from nadare.gui.downloads_page import DownloadRow, DownloadsPage


@pytest.fixture
def page(qapp, monkeypatch):
    widget = DownloadsPage()
    callbacks = []
    monkeypatch.setattr(
        widget._images, "request", lambda _urls, _w, _h, apply, **_kw: callbacks.append(apply)
    )
    yield widget, callbacks
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _row(cover):
    return DownloadRow("game", "Game", "install", "running", art=(cover,))


def _pixmap(color):
    image = QPixmap(32, 40)
    image.fill(QColor(color))
    return image


def test_late_cover_cannot_replace_newer_candidates(page):
    widget, callbacks = page
    widget.set_rows([_row("old-cover.png")])
    widget.set_rows([_row("new-cover.png")])
    assert len(callbacks) == 2
    callbacks[1](_pixmap("blue"))
    callbacks[0](_pixmap("red"))
    image = widget.row("game").thumb.pixmap().toImage()
    assert image.pixelColor(image.width() // 2, image.height() // 2) == QColor("blue")


def test_late_cover_cannot_update_a_replaced_row(page, monkeypatch):
    widget, callbacks = page
    widget.set_rows([_row("same-cover.png")])
    old_row = widget.row("game")
    writes = []
    monkeypatch.setattr(old_row, "set_art", writes.append)
    widget.set_rows([])
    widget.set_rows([_row("same-cover.png")])
    assert widget.row("game") is not old_row
    callbacks[0](_pixmap("red"))
    assert writes == []
