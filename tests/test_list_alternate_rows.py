# SPDX-License-Identifier: GPL-3.0-or-later
"""Alternate list rows are dark too (maintainer's Flatpak, 2026-11-11: every other row of the
logs list was near-white with unreadable text until hovered).

The logs list uses alternating row colours; the theme never set ``alternate-background-color``, so
Qt painted those rows from the platform palette. In the Flatpak there is no KDE colour scheme, so
that is Qt's light default. The offscreen platform has the same light default, so it reproduces.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QListWidget  # noqa: E402

from nadare.gui import theme  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_every_row_of_an_alternating_list_is_dark(qapp):
    qapp.setStyleSheet(theme.stylesheet())
    lst = QListWidget()
    try:
        lst.setAlternatingRowColors(True)
        lst.addItems(["arknights · background", "arknights · launch", "arknights · install"])
        lst.resize(420, 200)
        lst.show()
        qapp.processEvents()
        image = lst.viewport().grab().toImage()
        for row in range(3):
            rect = lst.visualItemRect(lst.item(row))
            colour = QColor(image.pixel(rect.right() - 4, rect.center().y()))
            assert colour.lightness() < 80, (row, colour.name())
    finally:
        lst.deleteLater()
