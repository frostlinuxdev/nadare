# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from nadare.gui.sidebar import GameRow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_a_refreshed_title_keeps_its_tooltip_plain(qapp):
    row = GameRow("p", "Game")
    try:
        row.update_content({"title": "Game"}, "<b>Game</b>")
        assert "<b>" not in row.title_label.toolTip()
        assert "<b>" not in row.toolTip()
    finally:
        row.deleteLater()
        qapp.processEvents()
