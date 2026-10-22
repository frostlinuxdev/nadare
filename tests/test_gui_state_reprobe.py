# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page must never keep a stale READY state after a failure."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    return MainWindow(config)


@pytest.mark.parametrize("operation", ["install", "update"])
def test_second_click_on_cancel_does_not_abort_the_new_job(qapp, operation):
    view = GameView()
    actions: list[str] = []
    cancels: list[bool] = []
    view.action_requested.connect(actions.append)
    view.cancel_requested.connect(lambda: cancels.append(True))
    try:
        view.set_profile("alpha", {"title": "Alpha"})
        if operation == "install":
            view.set_status(Status("not_installed"))
        else:
            view.set_status(Status("installed", data={"update_available": True}))

        view._on_primary()  # first click of a double-click
        assert actions == [operation]
        view.set_busy(operation)  # the controller reports the new job immediately
        view._on_primary()  # second click lands on the freshly painted CANCEL
        assert cancels == [], "the double-click's second half cancelled the new job"

        # Once the grace window has passed, CANCEL works normally.
        view._primary_grace_until = time.monotonic() - 1
        view._on_primary()
        assert cancels == [True]
    finally:
        view.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("operation", ["launch", "update", "verify"])
def test_failed_operation_reprobes_and_drops_stale_ready(
    tmp_path, qapp, monkeypatch, operation
):
    window = _window(tmp_path)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.controller,
        "start",
        lambda profile, op, payload=None: started.append((profile, op)) or True,
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    try:
        window._on_result("alpha", Status("installed"))
        window.open_game("alpha")
        assert window.game.badge.text() == "READY"
        started.clear()

        window._on_failed("alpha", operation, "the game folder is gone")

        assert started == [("alpha", "status")]
        assert window._snapshots["alpha"]["status"].state == "error"
        assert window.game.badge.text() == "ERROR"
        assert window.game.primary.text() == "RETRY"
        assert window.game.primary.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_failed_status_probe_is_kept_and_can_be_retried(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.controller,
        "start",
        lambda profile, op, payload=None: started.append((profile, op)) or True,
    )
    try:
        window._on_result("alpha", Status("running", running=True))
        window._on_failed("alpha", "status", "could not check the game")

        assert window._snapshots["alpha"]["status"].state == "error"
        # A library rebuild (Add/Remove/Settings) must not replay the old RUNNING state.
        window.refresh_library()
        assert window.library._cards["alpha"].badge.text() == "ERROR"

        window.open_game("alpha")
        assert window.game.badge.text() == "ERROR"
        assert window.game.primary.text() == "RETRY"
        started.clear()
        window.game.primary.click()
        assert started == [("alpha", "status")]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_background_failure_dialog_names_the_game(tmp_path, qapp, monkeypatch):
    """#369: a failure for a game that is not on screen must say which game."""
    window = _window(tmp_path)
    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, *a, **k: titles.append(title)
    )
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    try:
        window._current_profile = "someone-else"
        window._on_failed("alpha", "install", "disk full")
        assert titles and "Alpha" in titles[0]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_switching_away_and_back_replays_a_running_install(tmp_path, qapp, monkeypatch):
    """#368: the console and progress of a running install survive a game switch."""
    from nadare.core.events import Event

    window = _window(tmp_path)
    window.config.add_profile("beta", backend="wine", title="Beta")
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    try:
        window.open_game("alpha")
        window._on_busy("alpha", "install", True)
        window._on_event("alpha", Event("log", "downloading runner"))
        window._on_event("alpha", Event("step", "Downloading", progress=0.5))
        window.open_game("beta")
        assert "downloading runner" not in window.game.console.toPlainText()
        window.open_game("alpha")
        assert "downloading runner" in window.game.console.toPlainText()
        assert window.game.progress.value() == 50
        # a fresh run starts with a clean log
        window._on_busy("alpha", "install", False)
        window._on_busy("alpha", "install", True)
        window.open_game("beta")
        window.open_game("alpha")
        assert "downloading runner" not in window.game.console.toPlainText()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
