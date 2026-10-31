# SPDX-License-Identifier: GPL-3.0-or-later
"""Library games that are not installed are dimmed in the sidebar, as Steam does."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui import theme
from nadare.gui.sidebar import GameRow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _row(qapp, status):
    row = GameRow("g", "Game")
    row.set_state(status)
    return row


def test_a_game_reported_not_installed_is_dimmed(qapp):
    row = _row(qapp, Status(state="not_installed"))
    assert row.title_label.property("dim") == "true"
    assert row.icon.graphicsEffect() is not None


def test_an_installed_or_running_game_is_not_dimmed(qapp):
    for state in ("installed", "running"):
        row = _row(qapp, Status(state=state))
        assert row.title_label.property("dim") == "false"
        assert row.icon.graphicsEffect() is None


def test_an_update_available_game_is_not_dimmed(qapp):
    row = _row(qapp, Status(state="not_installed", data={"update_available": True}))
    assert row.title_label.property("dim") == "false"


def test_a_game_with_no_status_yet_is_not_dimmed(qapp):
    row = _row(qapp, None)
    assert row.title_label.property("dim") == "false"


def test_a_game_going_from_not_installed_to_installed_loses_the_dim(qapp):
    row = _row(qapp, Status(state="not_installed"))
    row.set_state(Status(state="installed"))
    assert row.title_label.property("dim") == "false"
    assert row.icon.graphicsEffect() is None


def test_the_stylesheet_has_a_dim_rule_for_the_title():
    assert 'QLabel#GameRowTitle[dim="true"]' in theme.STYLESHEET


def test_an_installed_title_is_brighter_than_a_not_installed_one():
    sheet = theme.STYLESHEET
    assert f"QLabel#GameRowTitle {{ color: {theme.TEXT_SOFT};" in sheet
    assert 'QLabel#GameRowTitle[dim="true"] { color: #566073; }' in sheet


def test_a_plain_text_box_is_themed_not_left_white():
    # The Environment editor is a QPlainTextEdit; unthemed it is white on a light desktop.
    assert "QPlainTextEdit {" in theme.STYLESHEET.replace("QPlainTextEdit#Console {", "")
