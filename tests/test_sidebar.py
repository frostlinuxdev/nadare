# SPDX-License-Identifier: GPL-3.0-or-later
"""Offscreen tests for the sidebar's installed-games list.

Only games the backend last reported as installed (or running / update
available) appear in the sidebar; status results add and remove rows live.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.main_window import PAGE_GAME, MainWindow


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    """A window with three profiles and a controller that never runs a backend."""
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController,
        "start",
        lambda self, profile, operation, payload=None: True,
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.add_profile("beta", backend="wine", title="Beta")
    config.add_profile("gamma", backend="wine", title="Gamma")
    win = MainWindow(config)
    qtbot.addWidget(win)
    return win


def test_only_installed_profiles_are_listed(window):
    window._on_result("alpha", Status("installed"))
    window._on_result("beta", Status("not_installed"))
    window._on_result("gamma", Status("error"))

    assert window.sidebar_games.profiles() == ["alpha"]
    assert window.sidebar_games.row("beta") is None


def test_status_change_adds_and_removes_rows(window):
    window._on_result("alpha", Status("not_installed"))
    assert window.sidebar_games.profiles() == []

    window._on_result("alpha", Status("installed"))
    assert window.sidebar_games.profiles() == ["alpha"]

    window._on_result("alpha", Status("not_installed"))
    assert window.sidebar_games.profiles() == []


def test_running_games_sort_first_then_alphabetically(window):
    window._on_result("gamma", Status("installed"))
    window._on_result("alpha", Status("installed"))
    window._on_result("beta", Status("running", running=True))

    assert window.sidebar_games.profiles() == ["beta", "alpha", "gamma"]


def test_running_and_update_dots(window):
    window._on_result("alpha", Status("running", running=True))
    running = window.sidebar_games.row("alpha")
    assert running is not None
    assert running.dot.property("kind") == "running"
    assert not running.dot.isHidden()

    window._on_result("beta", Status("installed", data={"update_available": True}))
    update = window.sidebar_games.row("beta")
    assert update is not None
    assert update.dot.property("kind") == "update"
    assert not update.dot.isHidden()


def test_click_opens_the_game_page(window, qtbot):
    window.show()
    window.show_library()
    window._on_result("alpha", Status("installed"))
    qtbot.wait(10)

    row = window.sidebar_games.row("alpha")
    assert row is not None
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)

    assert window._current_profile == "alpha"
    assert window.stack.currentIndex() == PAGE_GAME
    assert row.property("active") == "true"


def test_click_that_rebuilds_rows_does_not_crash(window, qtbot):
    window.show()
    window.show_library()
    window._on_result("alpha", Status("installed"))
    qtbot.wait(10)

    row = window.sidebar_games.row("alpha")
    assert row is not None
    # Opening a game can trigger a synchronous status refresh that rebuilds the
    # list while the row's mouse handler is still on the stack.
    row.clicked.connect(
        lambda _profile: window.sidebar_games.set_status(
            "alpha", Status("running", running=True)
        )
    )
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)

    assert window.stack.currentIndex() == PAGE_GAME
    assert window.sidebar_games.row("alpha") is row


def test_busy_dialog_only_for_foreground_operations(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    dialogs: list[tuple] = []
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: False)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: dialogs.append(a))

    window._start("alpha", "launch")
    assert len(dialogs) == 1

    # A background refresh that loses the race is skipped silently.
    window._start("alpha", "status", silent=True)
    assert len(dialogs) == 1


def test_active_row_follows_open_game(window):
    window._on_result("alpha", Status("installed"))
    window._on_result("beta", Status("installed"))
    window.open_game("alpha")
    assert window.sidebar_games.row("alpha").property("active") == "true"
    assert window.sidebar_games.row("beta").property("active") == "false"

    window.open_game("beta")
    assert window.sidebar_games.row("beta").property("active") == "true"
    assert window.sidebar_games.row("alpha").property("active") == "false"
