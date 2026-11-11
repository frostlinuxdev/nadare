# SPDX-License-Identifier: GPL-3.0-or-later
"""Keyboard focus can be seen on every kind of control (a Tab press must show where it went)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton, QVBoxLayout, QWidget
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import theme


@pytest.fixture(scope="module")
def qapp():
    # The host widget gets the theme itself; an app-wide restyle touches other tests' widgets.
    return QApplication.instance() or QApplication([])


def _looks_different_with_focus(qapp, widget) -> bool:
    host = QWidget()
    host.setObjectName("Root")
    host.setStyleSheet(theme.stylesheet())
    layout = QVBoxLayout(host)
    other = QPushButton("other")
    layout.addWidget(other)
    layout.addWidget(widget)
    host.resize(320, 140)
    host.show()
    qapp.processEvents()
    other.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    without = widget.grab().toImage()
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    assert widget.hasFocus()
    with_focus = widget.grab().toImage()
    host.close()
    host.deleteLater()
    return without != with_focus


@pytest.mark.parametrize("name", ["GhostButton", "Primary", "Danger", "NavItem"])
def test_styled_buttons_show_focus(qapp, name):
    button = QPushButton("Button")
    button.setObjectName(name)
    assert _looks_different_with_focus(qapp, button), name


def test_a_checkbox_shows_focus(qapp):
    assert _looks_different_with_focus(qapp, QCheckBox("Sandbox: hide my home folder"))


def test_the_check_itself_sees_a_plain_buttons_focus(qapp):
    """Control: the plain button already had a focus border, so this must pass before and after."""
    assert _looks_different_with_focus(qapp, QPushButton("Plain"))


def test_a_ticked_checkbox_shows_focus_too(qapp):
    box = QCheckBox("Remember me on this device")
    box.setChecked(True)
    assert _looks_different_with_focus(qapp, box)
