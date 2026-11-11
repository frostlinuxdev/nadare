# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b231: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.gui.sidebar import SidebarGames


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_a_running_game_is_not_hidden_as_uninstalled(qapp):
    side = SidebarGames()
    try:
        side.set_hide_uninstalled(True)
        side.set_profiles({"fgo": {"title": "FGO"}})
        # A process can be running while the state word still says not_installed
        # (the FGO backend picks state from its adapter, not from running).
        side.set_status("fgo", Status(state="not_installed", running=True))
        assert side.profiles() == ["fgo"]
    finally:
        side.deleteLater()
        qapp.processEvents()
