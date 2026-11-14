# SPDX-License-Identifier: GPL-3.0-or-later
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


def test_a_late_status_does_not_hide_a_readded_game(qapp):
    side = SidebarGames()
    try:
        side.set_hide_uninstalled(True)
        side.set_profiles({"a": {"title": "A"}})
        side.set_profiles({})  # the profile is removed
        side.set_status("a", Status(state="not_installed"))  # its worker reports late
        side.set_profiles({"a": {"title": "A"}})  # and it is added back
        assert side.row("a") is not None
    finally:
        side.deleteLater()
        qapp.processEvents()
