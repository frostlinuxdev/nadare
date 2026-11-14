# SPDX-License-Identifier: GPL-3.0-or-later
"""W2-D2: an interrupted install (installed + incomplete) must not read as READY anywhere."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.sidebar import is_installed


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_an_incomplete_install_is_shown_as_not_installed_and_hidden(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="direct-bd2", title="Alpha")
    config.add_profile("beta", backend="direct-bd2", title="Beta")
    window = MainWindow(config)
    try:
        status = Status("installed", data={"incomplete": True})
        window._on_result("alpha", status)
        card = window.library._cards["alpha"]
        seen = {
            "sidebar_installed": is_installed(status),
            "card_badge_hidden": card.badge.isHidden(),
            "card_badge": card.badge.text(),
            "card_play_hidden": card.play_button.isHidden(),
            "page_badge": window.game.badge.text(),
            "page_badge_hidden": window.game.badge.isHidden(),
            "page_primary": window.game.primary.text(),
        }
        window.open_game("alpha")
        window._on_result("alpha", status)
        seen["page_badge"] = window.game.badge.text()
        seen["page_badge_hidden"] = window.game.badge.isHidden()
        seen["page_primary"] = window.game.primary.text()
        window.show_library()
        window.sidebar_games.set_hide_uninstalled(True)
        seen["hidden"] = "alpha" not in window.sidebar_games.profiles()
        assert seen == {
            "sidebar_installed": False,
            "card_badge_hidden": False,
            "card_badge": "NOT INSTALLED",
            "card_play_hidden": True,
            "page_badge": "NOT INSTALLED",
            "page_badge_hidden": False,
            "page_primary": "RESUME",
            "hidden": True,
        }, seen
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
