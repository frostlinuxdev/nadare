# SPDX-License-Identifier: GPL-3.0-or-later
"""Sweep b23r1 c30: "Logged in as …" of the previous game stayed on the next game's page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_account_line_belongs_to_one_game(qapp):
    view = GameView()
    try:
        view.set_profile("a", {"backend": "stove-czn"})
        view.set_status(Status("installed", data={"logged_in": True, "account": "Cat"}))
        assert "Cat" in view.account_label.text()
        view.set_profile("b", {"backend": "netmarble-mongil"})
        assert view.account_label.isHidden()
        assert "Cat" not in view.account_label.text()
    finally:
        view.deleteLater()
