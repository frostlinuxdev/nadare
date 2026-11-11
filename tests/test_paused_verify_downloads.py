# SPDX-License-Identifier: GPL-3.0-or-later
"""A paused download survives Verify; Downloads rows fit the window and open by keyboard (#855)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.downloads_page import DownloadRow, DownloadRowWidget
from nadare.gui.game_view import GameView
from nadare.gui.workers import BackendController


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("g", {"backend": "direct-bd2", "title": "G"})
    page.set_status(Status("installed", data={"can_repair": True, "can_update": True}))
    yield page
    page.deleteLater()
    qapp.processEvents()


# ---- Verify and Repair would throw the paused download away ------------------------------
def test_verify_and_repair_are_off_while_a_download_is_paused(view):
    assert view.verify.isEnabled() and view.repair_btn.isEnabled()
    view.set_paused("update")
    assert not view.verify.isEnabled() and not view.repair_btn.isEnabled()
    assert "paused" in view.verify.toolTip().lower()


def test_they_come_back_when_nothing_is_paused(view):
    view.set_paused("update")
    view._paused_op = None
    view._refresh_actions()
    assert view.verify.isEnabled() and view.repair_btn.isEnabled()


def test_the_controller_keeps_a_paused_operation_when_a_check_is_asked_for(qapp):
    controller = BackendController(None)
    controller._paused["g"] = ("update", None)
    assert controller.start("g", "verify") is False
    assert controller.paused_operation("g") == "update"
    assert controller.start("g", "repair") is False
    assert controller.paused_operation("g") == "update"


# ---- long titles ---------------------------------------------------------------------------
def _row(title: str) -> DownloadRowWidget:
    widget = DownloadRowWidget("g")
    row = DownloadRow("g", title, "update", "running", detail="x", fraction=0.3, eta="5 min")
    widget.apply(row)
    return widget


def test_a_very_long_title_does_not_widen_the_row(qapp):
    long_title = "A very long custom title " * 20
    widget = _row(long_title)
    assert widget.minimumSizeHint().width() < 700
    assert widget.title_label.toolTip() == long_title


def test_a_short_title_is_shown_in_full(qapp):
    widget = _row("Wuthering Waves")
    assert widget.title_label.text() == "Wuthering Waves"


# ---- keyboard ------------------------------------------------------------------------------
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space])
def test_a_row_opens_from_the_keyboard(qapp, key):
    widget = _row("Game")
    opened = []
    widget.clicked.connect(lambda: opened.append(1))
    widget.show()
    widget.setFocus()
    QTest.keyClick(widget, key)
    assert opened == [1]
    widget.hide()


def test_a_row_can_take_focus_and_says_what_it_is(qapp):
    widget = _row("Game")
    assert widget.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert "Game" in widget.accessibleName()


def test_the_usual_tooltips_come_back_after_a_pause(view):
    verify_tip, repair_tip = view.verify.toolTip(), view.repair_btn.toolTip()
    assert verify_tip.startswith("Compares the installed game files")
    view.set_paused("update")
    assert "paused" in view.verify.toolTip().lower()
    assert "paused" in view.repair_btn.toolTip().lower()
    view._paused_op = None
    view._refresh_actions()
    assert view.verify.toolTip() == verify_tip and view.repair_btn.toolTip() == repair_tip


# ---- Update would throw the paused download away too (b23 round 4) -------------------------
def test_update_is_off_while_a_download_is_paused(view):
    assert view.update_btn.isEnabled()
    view.set_paused("repair")
    assert not view.update_btn.isEnabled()
    assert "paused" in view.update_btn.toolTip().lower()
    view._paused_op = None
    view._refresh_actions()
    assert view.update_btn.isEnabled()


def test_the_controller_keeps_a_paused_operation_when_update_is_asked_for(qapp):
    controller = BackendController(None)
    controller._paused["g"] = ("repair", None)
    assert controller.start("g", "update") is False
    assert controller.paused_operation("g") == "repair"
