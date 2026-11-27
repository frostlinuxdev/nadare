# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Shared sizing for dialogs with word-wrapped text.

Qt computes a dialog's initial size hint from each wrapped ``QLabel``'s preferred
(wide) width and then clamps the dialog to a narrower width without asking the
layout for ``heightForWidth`` at that width, clipping the labels vertically.
Long text or large desktop font sizes can also make a dialog taller than a
small screen. The helpers here give a dialog a frameless vertical scroll body
when needed and size it to the exact wrapped height that fits the screen while
keeping action buttons outside the scroll area.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

#: Qt's QWIDGETSIZE_MAX: no maximum size.
_UNLIMITED = 16_777_215


class _ScrollBody(QScrollArea):
    """Frameless scroll area whose minimum hint does not pad short dialogs."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        widget = self.widget()
        return widget is not None and widget.layout() is not None

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        widget = self.widget()
        if widget is None or widget.layout() is None:
            return super().heightForWidth(width)
        vp_w = max(1, width - self.frameWidth() * 2)
        return layout_height_for_width(widget.layout(), vp_w) + self.frameWidth() * 2


def make_scroll_body(spacing: int = 12) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    """Build a vertical-only ``QScrollArea`` and its zero-margin content layout."""
    scroll = _ScrollBody()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    scroll.setWidget(content)
    return scroll, content, layout


def _polish_tree(widget: QWidget) -> None:
    """Apply stylesheet fonts before measuring, even when the dialog is not shown yet."""
    widget.ensurePolished()
    for child in widget.findChildren(QWidget):
        if not child.isHidden():
            child.ensurePolished()


def _layout_has_visible(layout: QLayout) -> bool:
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            if not widget.isHidden():
                return True
        else:
            sub = item.layout()
            if sub is not None and _layout_has_visible(sub):
                return True
    return False


def _item_height_for_width(item: QLayoutItem, width: int) -> int:
    widget = item.widget()
    if widget is not None:
        if widget.isHidden():
            return 0
        height = (
            widget.heightForWidth(width)
            if widget.hasHeightForWidth()
            else widget.sizeHint().height()
        )
        return max(height, widget.minimumSizeHint().height(), 0)
    sub = item.layout()
    if sub is not None:
        if not _layout_has_visible(sub):
            return 0
        height = (
            sub.totalHeightForWidth(width) if sub.hasHeightForWidth() else sub.sizeHint().height()
        )
        return max(height, sub.minimumSize().height(), 0)
    return 0


def layout_height_for_width(layout: QLayout, width: int, *, exclude: QWidget | None = None) -> int:
    """Exact height ``layout`` needs at ``width`` (honouring every wrapped child).

    When ``exclude`` is given (a scroll area), it still counts toward inter-item
    spacing while contributing zero height itself, giving the chrome height.
    """
    margins = layout.contentsMargins()
    inner_w = max(1, width - margins.left() - margins.right())
    spacing = max(0, layout.spacing())
    total = margins.top() + margins.bottom()
    visible = 0
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            if widget.isHidden():
                continue
            visible += 1
            if widget is exclude:
                continue
            total += _item_height_for_width(item, inner_w)
            continue
        sub = item.layout()
        if sub is not None and _layout_has_visible(sub):
            visible += 1
            total += _item_height_for_width(item, inner_w)
    if visible > 1:
        total += spacing * (visible - 1)
    return total


def _min_dialog_width(outer: QLayout, min_width: int, scroll: QScrollArea | None) -> int:
    """Minimum width needed by non-scroll rows (such as the button bar) plus margins."""
    margins = outer.contentsMargins()
    need = min_width
    for i in range(outer.count()):
        item = outer.itemAt(i)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            if widget.isHidden() or widget is scroll:
                continue
            row_w = widget.minimumSizeHint().width()
        else:
            sub = item.layout()
            if sub is None or not _layout_has_visible(sub):
                continue
            row_w = sub.minimumSize().width()
        need = max(need, row_w + margins.left() + margins.right())
    return need


def fit_dialog(
    dialog: QDialog,
    *,
    min_width: int,
    scroll: QScrollArea | None = None,
    fixed_size: bool = False,
    keep_width: bool = False,
    margin_x: int = 40,
    margin_y: int = 60,
) -> None:
    """Size ``dialog`` to the height its wrapped text needs while fitting the screen.

    When ``scroll`` is given, its body scrolls only if the wrapped content is taller than
    the available screen height, keeping header and action buttons outside ``scroll`` visible.
    When ``keep_width`` is true (later shows after the first), keep any wider width the user
    gave the dialog and only refit its height.
    """
    outer = dialog.layout()
    if outer is None:
        return
    dialog.setMinimumSize(min_width, 0)
    dialog.setMaximumSize(_UNLIMITED, _UNLIMITED)
    _polish_tree(dialog)

    screen = dialog.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
    max_w = min(avail.width(), max(min_width, avail.width() - margin_x))
    max_h = min(avail.height(), max(240, avail.height() - margin_y))

    try:
        base_w = _min_dialog_width(outer, min_width, scroll)
        if keep_width and not fixed_size:
            base_w = max(base_w, dialog.width())
        target_w = min(base_w, max_w)
        if scroll is None or scroll.widget() is None or scroll.widget().layout() is None:
            need_h = layout_height_for_width(outer, target_w)
            target_h = min(max(need_h, outer.minimumSize().height()), max_h)
            dialog.resize(target_w, target_h)
            outer.activate()
            return

        content = scroll.widget()
        content_layout = content.layout()
        chrome_h = layout_height_for_width(outer, target_w, exclude=scroll)
        margins = outer.contentsMargins()
        inner_w = max(1, target_w - margins.left() - margins.right())
        vp_w = max(1, inner_w - scroll.frameWidth() * 2)
        content_h = layout_height_for_width(content_layout, vp_w)
        target_h = min(chrome_h + content_h, max_h)
        dialog.resize(target_w, target_h)
        outer.activate()
        content_layout.activate()
    finally:
        if fixed_size:
            dialog.setFixedSize(dialog.size())


class _CheckLabel(QLabel):
    """Word-wrapped plain-text buddy label that toggles its ``QCheckBox`` on click."""

    def __init__(self, text: str, checkbox: QCheckBox, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._checkbox = checkbox
        self._pressed = False
        self.setObjectName("Hint")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._checkbox.isEnabled():
            self._pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            if self._checkbox.isEnabled() and self.rect().contains(event.position().toPoint()):
                self._checkbox.toggle()
            event.accept()
            return
        self._pressed = False
        super().mouseReleaseEvent(event)


class WrappedCheckBox(QWidget):
    """Checkbox whose label wraps across lines instead of clipping at the right edge."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

        self.checkbox = QCheckBox("")
        self.checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.checkbox.setAccessibleName(text)
        self.label = _CheckLabel(text, self.checkbox)
        self.toggled = self.checkbox.toggled
        self.setFocusProxy(self.checkbox)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        row.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.label, 1)

    def isChecked(self) -> bool:  # noqa: N802 - Qt QCheckBox API
        return self.checkbox.isChecked()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt QCheckBox API
        self.checkbox.setChecked(checked)

    def toggle(self) -> None:
        self.checkbox.toggle()

    def click(self) -> None:
        self.checkbox.click()

    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt QCheckBox API
        self.label.setText(text)
        self.checkbox.setAccessibleName(text)
        self.updateGeometry()

    def setToolTip(self, tip: str) -> None:  # noqa: N802 - Qt override
        super().setToolTip(tip)
        self.checkbox.setToolTip(tip)
        self.label.setToolTip(tip)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        cb_size = self.checkbox.sizeHint()
        layout = self.layout()
        spacing = layout.spacing() if layout is not None else 9
        label_w = max(1, width - cb_size.width() - spacing)
        return max(cb_size.height(), self.label.heightForWidth(label_w))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        cb_size = self.checkbox.sizeHint()
        lbl_size = self.label.sizeHint()
        layout = self.layout()
        spacing = layout.spacing() if layout is not None else 9
        return QSize(
            cb_size.width() + spacing + lbl_size.width(),
            max(cb_size.height(), lbl_size.height()),
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        cb_size = self.checkbox.sizeHint()
        lbl_min = self.label.minimumSizeHint()
        layout = self.layout()
        spacing = layout.spacing() if layout is not None else 9
        return QSize(
            cb_size.width() + spacing + lbl_min.width(),
            max(cb_size.height(), lbl_min.height()),
        )


__all__ = ["WrappedCheckBox", "fit_dialog", "layout_height_for_width", "make_scroll_body"]
