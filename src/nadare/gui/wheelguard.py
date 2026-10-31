# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep the mouse wheel from changing a dropdown or number box nobody clicked (#774).

Scrolling a long page with the wheel used to change whichever dropdown the pointer happened to
cross (a renderer or runner changed with no click). A dropdown or number box now answers the
wheel only while it has keyboard focus, which a click or a Tab gives it; otherwise the wheel
goes on to the page behind it.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)


def _guarded(obj: object) -> bool:
    return isinstance(obj, (QComboBox, QAbstractSpinBox))


class WheelGuard(QObject):
    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: N802
        if (
            event is not None
            and event.type() == QEvent.Type.Polish
            and isinstance(obj, QWidget)
            and _guarded(obj)
        ):
            # Qt hands focus to a widget with wheel focus *before* the wheel reaches this filter,
            # which would make the dropdown count as focused. A click or Tab still focuses it.
            obj.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            return False
        if (
            event is not None
            and event.type() == QEvent.Type.Wheel
            and _guarded(obj)
            and not obj.hasFocus()
        ):
            # An application-wide filter runs before Qt's own hand-over to the parent widget, and
            # a hand-over from here does not climb either, so the wheel is given straight to
            # the scroll area the dropdown sits in (when there is one).
            area = obj.parentWidget()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parentWidget()
            if area is not None and isinstance(event, QWheelEvent):
                viewport = area.viewport()
                if viewport is not None:
                    position = QPointF(obj.mapTo(viewport, event.position().toPoint()))
                    QApplication.sendEvent(
                        viewport,
                        QWheelEvent(
                            position,
                            event.globalPosition(),
                            event.pixelDelta(),
                            event.angleDelta(),
                            event.buttons(),
                            event.modifiers(),
                            event.phase(),
                            event.inverted(),
                        ),
                    )
            event.accept()
            return True
        return False


def install(app: QApplication) -> WheelGuard:
    """Install the guard once for the whole application."""
    guard = getattr(app, "_nadare_wheel_guard", None)
    if guard is None:
        guard = WheelGuard(app)
        app.installEventFilter(guard)
        app._nadare_wheel_guard = guard
    return guard


def install_for(app: object) -> WheelGuard | None:
    """:func:`install` for a real ``QApplication``; any other object is left alone."""
    return install(app) if isinstance(app, QApplication) else None
