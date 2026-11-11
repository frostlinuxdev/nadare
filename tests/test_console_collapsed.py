# SPDX-License-Identifier: GPL-3.0-or-later
"""#993: the OUTPUT console starts folded away, stays as the user leaves it, opens on errors."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    widget = GameView()
    widget.set_profile("sample", {"backend": "stove-czn"})
    yield widget
    widget.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("operation", ["launch", "login", "install", "verify"])
def test_an_operation_starts_with_the_output_folded(view, operation):
    view.set_busy(operation)
    view.append_event(Event("log", "a line"))
    assert view.console.isHidden()
    assert not view.output_head.isHidden()  # "Show output" is there to open it
    assert view.console_toggle.text() == "Show output"


def test_an_error_opens_the_output(view):
    view.set_busy("launch")
    view.append_event(Event("error", "boom"))
    assert not view.console.isHidden()


def test_the_users_choice_sticks_across_operations_and_games(view):
    chosen: list[bool] = []
    view.console_preference_changed.connect(chosen.append)
    view.append_event(Event("log", "a line"))
    view.console_toggle.click()
    assert chosen == [True]
    view.set_busy("launch")
    view.append_event(Event("log", "another"))
    assert not view.console.isHidden()
    view.set_profile("other", {"backend": "stove-czn"})
    view.append_event(Event("log", "on the next game"))
    assert not view.console.isHidden()
    view.console_toggle.click()
    assert chosen == [True, False]
    view.set_busy("launch")
    view.append_event(Event("log", "folded again"))
    assert view.console.isHidden()


def test_the_main_window_saves_and_restores_the_choice(tmp_path: Path, qapp, monkeypatch):
    from nadare.gui.main_window import MainWindow
    from nadare.gui.workers import BackendController

    monkeypatch.setattr(BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", title="Sample")
    config.save()
    window = MainWindow(config)
    try:
        window.game.append_event(Event("log", "a line"))
        window.game.console_toggle.click()
        assert Config.load(tmp_path / "config.json").data.get("output_expanded") is True
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
    again = MainWindow(Config.load(tmp_path / "config.json"))
    try:
        again.game.append_event(Event("log", "a line"))
        assert not again.game.console.isHidden()
    finally:
        again.close()
        again.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("operation", ["install", "update", "repair", "verify"])
def test_a_finished_operation_says_so_outside_the_folded_output(view, operation):
    """With OUTPUT folded, the "done" line must not be the only sign it worked."""
    view.set_busy(operation)
    view.append_event(Event("done", "NIKKE installer finished"))
    view.finish(operation, 0)
    assert view.step_label.text() == "✓ NIKKE installer finished"
    assert not view.progress_frame.isHidden()
    view.set_busy("launch")
    assert "✓" not in view.step_label.text()


def test_a_finished_operation_without_a_done_line_still_says_it_finished(view):
    view.set_busy("repair")
    view.finish("repair", 0)
    assert view.step_label.text().startswith("✓ ")


def test_a_failed_operation_shows_no_tick(view):
    view.set_busy("install")
    view.append_event(Event("done", "half"))
    view.finish("install", 1)
    assert "✓" not in view.step_label.text()
