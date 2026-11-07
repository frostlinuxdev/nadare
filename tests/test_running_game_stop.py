# SPDX-License-Identifier: GPL-3.0-or-later
"""A running game shows PLAYING with a small stop (x) button, like Steam (b21 polish).

The red STOP used to be the big primary button, so one mis-click ended the session.
"""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _answer(monkeypatch, button):
    asked: list[str] = []

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return button

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return asked


def _view(qapp, *, title="Brown Dust 2"):
    view = GameView()
    view.set_profile("p", {"title": title})
    return view


def test_a_launch_in_progress_shows_playing_and_the_stop_button(qapp):
    view = _view(qapp)
    view.set_status(Status("installed"))
    view.set_busy("launch")
    assert view.primary.text() == "PLAYING"
    assert not view.primary.isEnabled()
    assert not view.stop_button.isHidden()
    view.deleteLater()


def test_opening_a_game_that_is_already_running_shows_the_same(qapp):
    view = _view(qapp)
    view.set_status(Status("running", running=True))
    assert view.primary.text() == "PLAYING"
    assert not view.primary.isEnabled()
    assert not view.stop_button.isHidden()
    view.deleteLater()


def test_the_stop_button_is_hidden_when_nothing_is_running(qapp):
    view = _view(qapp)
    view.set_status(Status("installed"))
    assert view.primary.text() == "LAUNCH"
    assert view.stop_button.isHidden()
    view.set_busy("install")
    assert view.stop_button.isHidden()
    view.deleteLater()


def test_clicking_the_primary_while_playing_does_nothing(qapp):
    view = _view(qapp)
    actions: list[str] = []
    cancels: list[bool] = []
    view.action_requested.connect(actions.append)
    view.cancel_requested.connect(lambda: cancels.append(True))
    view.set_status(Status("running", running=True))
    view.primary.click()
    assert actions == [] and cancels == []
    view.deleteLater()


def test_stop_asks_first_and_names_the_game(qapp, monkeypatch):
    asked = _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    view = _view(qapp)
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_status(Status("running", running=True))
    view._primary_grace_until = 0.0
    view.stop_button.click()
    assert actions == ["stop"]
    assert len(asked) == 1 and "Brown Dust 2" in asked[0]
    view.deleteLater()


def test_keep_playing_does_not_stop_the_game(qapp, monkeypatch):
    _answer(monkeypatch, QMessageBox.StandardButton.No)
    view = _view(qapp)
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_status(Status("running", running=True))
    view._primary_grace_until = 0.0
    view.stop_button.click()
    assert actions == []
    view.deleteLater()


def test_the_stop_button_ignores_the_click_that_started_the_launch(qapp, monkeypatch):
    asked = _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    view = _view(qapp)
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_busy("launch")  # the double-click grace window is open
    view.stop_button.click()
    assert actions == [] and asked == []
    view.deleteLater()


def test_the_stop_button_is_named_for_screen_readers(qapp):
    view = _view(qapp)
    assert view.stop_button.accessibleName() == "Stop game"
    assert view.stop_button.toolTip() == "Stop game"
    view.deleteLater()
