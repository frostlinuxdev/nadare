# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate 5: the default click hold respects the desktop interval and safe bounds."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from nadare.gui import clickguard


@pytest.mark.parametrize(
    ("desktop_interval", "expected"), [(0, 450), (100, 300), (450, 450), (1200, 800)]
)
def test_default_question_hold_uses_a_bounded_desktop_interval(
    qapp, monkeypatch, desktop_interval, expected
):
    monkeypatch.setattr(QApplication, "doubleClickInterval", lambda: desktop_interval)
    box = QMessageBox()
    try:
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        clickguard.hold_clicks(box)
        timer = box.findChild(QTimer)
        assert timer is not None and timer.isActive()
        assert timer.interval() == expected
        assert not any(button.isEnabled() for button in box.buttons())
    finally:
        box.deleteLater()
