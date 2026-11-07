# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings returns to the place it was opened from, not always to the Library."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core.config import Config  # noqa: E402
from nadare.gui.main_window import PAGE_GAME, PAGE_LIBRARY, MainWindow  # noqa: E402


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from nadare.gui import workers

    monkeypatch.setattr(workers.BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.add_profile("a", backend="wine", title="A")
    config.add_profile("b", backend="wine", title="B")
    win = MainWindow(config)
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_saving_returns_to_the_game_it_was_opened_from(window):
    window.open_game("b")
    window.open_settings()
    window.settings.saved.emit("b")
    assert window.stack.currentIndex() == PAGE_GAME
    assert window._current_profile == "b"


def test_going_back_returns_to_the_game_it_was_opened_from(window):
    window.open_game("b")
    window.open_settings()
    window.settings.cancelled.emit()
    assert window.stack.currentIndex() == PAGE_GAME
    assert window._current_profile == "b"


def test_settings_opened_from_the_library_goes_back_to_the_library(window):
    window.show_library()
    window.open_settings()  # no game is open: these are Nadare's own settings
    window.settings.saved.emit("")
    assert window.stack.currentIndex() == PAGE_LIBRARY
    window.show_library()
    window.open_settings()
    window.settings.cancelled.emit()
    assert window.stack.currentIndex() == PAGE_LIBRARY


def test_a_game_that_is_gone_falls_back_to_the_library(window):
    window.open_game("b")
    window.open_settings()
    window.config.data["profiles"].pop("b")
    window.settings.saved.emit("b")
    assert window.stack.currentIndex() == PAGE_LIBRARY


def test_the_origin_is_not_remembered_for_a_later_visit(window):
    window.open_game("b")
    window.open_settings()
    window.settings.cancelled.emit()
    window.show_library()
    window.open_settings()
    window.settings.cancelled.emit()
    assert window.stack.currentIndex() == PAGE_LIBRARY
