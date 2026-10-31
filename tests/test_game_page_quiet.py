# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page shows nothing that carries no information (Heroic review, #778)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.events import Event
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    widget = GameView()
    widget.set_profile("sample", {"backend": "stove-czn"})
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def test_an_idle_installed_game_shows_no_empty_panels(view):
    view.set_status(Status("installed"))
    assert view.output_head.isHidden()
    assert view.console.isHidden()
    assert view.progress_frame.isHidden()
    assert view.detail_label.isHidden()
    assert view.badge.isHidden()
    assert view.kicker.isHidden()


def test_output_appears_with_the_first_line(view):
    view.append_event(Event("log", "hello"))
    assert not view.output_head.isHidden()
    assert not view.console.isHidden()


def test_clearing_the_output_hides_the_panel_again(view):
    view.append_event(Event("log", "hello"))
    view.console.clear()
    view._set_console_expanded(view._console_expanded)
    assert view.output_head.isHidden()


def test_a_running_operation_shows_progress_and_output(view):
    view.set_busy("install")
    assert not view.progress_frame.isHidden()
    assert not view.output_head.isHidden()
    view.finish("install", 0)
    assert view.progress_frame.isHidden()


def test_a_waiting_install_shows_the_queue_line(view):
    view.set_waiting("install", ahead=2)
    assert not view.progress_frame.isHidden()


def test_state_badges_other_than_ready_stay_visible(view):
    for state in ("error", "not_installed", "running", "checking"):
        view.set_status(Status(state, message="x"))
        assert not view.badge.isHidden(), state
    view.set_status(Status("installed"))
    assert view.badge.isHidden()


def test_a_status_message_shows_the_detail_line(view):
    view.set_status(Status("error", message="boom"))
    assert not view.detail_label.isHidden()
    assert view.detail_label.text() == "boom"


def test_the_kicker_only_says_now_playing(view):
    view.set_status(Status("running", running=True))
    assert not view.kicker.isHidden()
    assert view.kicker.text() == "NOW PLAYING"
    view.set_status(Status("installed"))
    assert view.kicker.isHidden()


@pytest.mark.parametrize("kind,hidden", [("steam", True), ("wine", True), ("stove-czn", False)])
def test_self_evident_backend_labels_are_hidden(qapp, kind, hidden):
    widget = GameView()
    try:
        widget.set_profile("sample", {"backend": kind})
        assert widget.backend_label.isHidden() is hidden
    finally:
        widget.deleteLater()
        qapp.processEvents()


def test_a_fresh_page_shows_no_kicker_before_any_status_is_known(view):
    assert view.kicker.isHidden()
