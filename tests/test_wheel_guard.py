# SPDX-License-Identifier: GPL-3.0-or-later
"""#774: the mouse wheel scrolls the page; it only changes a dropdown that has focus."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QDoubleSpinBox,
        QScrollArea,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import wheelguard


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    wheelguard.install(app)
    return app


def wheel(widget, delta: int = -120) -> None:
    centre = QPointF(widget.rect().center())
    event = QWheelEvent(
        centre,
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


@pytest.fixture
def page(qapp):
    """A tall scrolling page with a dropdown, two spin boxes and filler."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    content = QWidget()
    layout = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["Auto", "Vulkan", "OpenGL"])
    spin = QSpinBox()
    spin.setRange(0, 100)
    dspin = QDoubleSpinBox()
    dspin.setRange(0, 100)
    editable = QComboBox()
    editable.setEditable(True)
    editable.addItems(["Default", "dwproton-11.0-15", "other"])
    layout.addWidget(combo)
    layout.addWidget(spin)
    layout.addWidget(dspin)
    layout.addWidget(editable)
    for _ in range(60):
        layout.addWidget(QComboBox())  # filler so the page really scrolls
    area.setWidget(content)
    area.resize(300, 200)
    area.show()
    qapp.processEvents()
    area.combo, area.spin, area.dspin, area.editable = combo, spin, dspin, editable
    yield area
    area.close()
    area.deleteLater()
    qapp.processEvents()


def test_the_wheel_over_an_unfocused_dropdown_scrolls_the_page_instead(page, qapp):
    page.combo.clearFocus()
    page.setFocus()
    before = page.verticalScrollBar().value()
    wheel(page.combo, -120)
    qapp.processEvents()
    assert page.combo.currentIndex() == 0  # not changed
    assert page.verticalScrollBar().value() > before  # the page moved


def test_the_wheel_over_an_unfocused_spin_box_scrolls_the_page_instead(page, qapp):
    page.setFocus()
    before = page.verticalScrollBar().value()
    wheel(page.spin, -120)
    wheel(page.dspin, -120)
    qapp.processEvents()
    assert page.spin.value() == 0 and page.dspin.value() == 0
    assert page.verticalScrollBar().value() > before


def test_a_wheel_cannot_hand_focus_to_a_dropdown_or_number_box(page):
    """Qt gives focus to a widget with wheel focus *before* the wheel reaches the filter, which
    would make the dropdown count as focused and answer. Seen on a real desktop (#774)."""
    for widget in (page.combo, page.editable, page.spin, page.dspin):
        assert widget.focusPolicy() == Qt.FocusPolicy.StrongFocus, type(widget).__name__


def test_the_wheel_over_an_unfocused_editable_dropdown_scrolls_the_page(page, qapp):
    page.setFocus()
    before = page.verticalScrollBar().value()
    wheel(page.editable, -120)
    qapp.processEvents()
    assert page.editable.currentIndex() == 0
    assert page.verticalScrollBar().value() > before


def test_an_editable_dropdown_whose_text_field_has_focus_answers_the_wheel(page, qapp):
    page.editable.lineEdit().setFocus()
    qapp.processEvents()
    assert page.editable.hasFocus()
    wheel(page.editable, -120)
    assert page.editable.currentIndex() == 1


def test_a_dropdown_that_has_focus_still_answers_the_wheel(page, qapp):
    page.combo.setFocus()
    qapp.processEvents()
    assert page.combo.hasFocus()
    wheel(page.combo, -120)
    assert page.combo.currentIndex() == 1


def test_a_spin_box_that_has_focus_still_answers_the_wheel(page, qapp):
    page.spin.setFocus()
    qapp.processEvents()
    wheel(page.spin, 120)
    assert page.spin.value() == 1


def test_other_widgets_scroll_as_usual(page, qapp):
    before = page.verticalScrollBar().value()
    wheel(page.viewport(), -120)
    assert page.verticalScrollBar().value() > before


def test_installing_twice_adds_one_filter(qapp):
    first = wheelguard.install(qapp)
    second = wheelguard.install(qapp)
    assert first is second


def test_the_app_theme_installs_the_guard(monkeypatch):
    from nadare.gui import theme

    seen = []

    class FakeApp:
        def setStyleSheet(self, _sheet):  # noqa: N802 - Qt naming
            pass

    monkeypatch.setattr(theme, "stylesheet", lambda: "")
    monkeypatch.setattr(theme, "register_fonts", lambda: None)
    monkeypatch.setattr(wheelguard, "install_for", seen.append)
    fake = FakeApp()
    theme.apply_theme(fake)
    assert seen == [fake]


def test_only_a_real_application_gets_the_filter():
    assert wheelguard.install_for(object()) is None
