# SPDX-License-Identifier: GPL-3.0-or-later
"""Library cards must work for keyboard users, not only mouse users."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.library import AddGameCard, GameCard


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _press(widget, key):
    event = QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def test_game_card_is_focusable_and_named(qapp):
    card = GameCard("p", "My Game", "wine")
    try:
        assert card.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert card.accessibleName() == "My Game"
        assert ":focus" in card.styleSheet()
    finally:
        card.deleteLater()
        qapp.processEvents()


def test_add_game_card_is_focusable_and_named(qapp):
    card = AddGameCard()
    try:
        assert card.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert card.accessibleName() == "Add game"
        assert ":focus" in card.styleSheet()
    finally:
        card.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize(
    "key",
    [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space],
)
def test_game_card_actions_work_from_the_keyboard(qapp, key):
    card = GameCard("p", "My Game", "wine")
    opened: list[str] = []
    card.clicked.connect(opened.append)
    try:
        _press(card, key)
        assert opened == ["p"]
    finally:
        card.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize(
    "key",
    [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space],
)
def test_add_game_card_actions_work_from_the_keyboard(qapp, key):
    card = AddGameCard()
    opened: list[bool] = []
    card.clicked.connect(lambda: opened.append(True))
    try:
        _press(card, key)
        assert opened == [True]
    finally:
        card.deleteLater()
        qapp.processEvents()
