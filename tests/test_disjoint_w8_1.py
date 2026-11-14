# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-1: the library card must not offer Play when the same status offers an update."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.gui.library import GameCard
from nadare.gui.sidebar import GameRow


def test_card_agrees_with_sidebar_about_an_available_update():
    qapp = QApplication.instance() or QApplication([])
    status = Status(
        state="installed",
        version="3.7.2",
        running=False,
        data={"version": "3.7.2", "update_available": True, "latest_version": "3.8.0"},
    )

    row = GameRow("wuwa", "Wuthering Waves")
    row.set_state(status)
    card = GameCard("wuwa", "Wuthering Waves", "direct-wuwa")
    # What MainWindow._on_result passes (the whole Status, since #1316).
    card.set_state(status)

    print(
        "sidebar dot hidden:", row.dot.isHidden(),
        "| card play hidden:", card.play_button.isHidden(),
        "| card badge:", repr(card.badge.text()),
        "| card badge hidden:", card.badge.isHidden(),
    )
    qapp.processEvents()

    assert not row.dot.isHidden(), "fixture: sidebar should show the update dot"
    assert card.play_button.isHidden(), (
        "Library card still says Play for a game whose status says update_available"
    )
    assert not card.badge.isHidden(), "Library card hides the update badge too"
