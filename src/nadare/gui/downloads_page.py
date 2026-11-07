# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Downloads page: every running, waiting and paused install in one list (#773).

Presentation only. The main window works out the rows from the controller and says what each
button means; this page lays them out and reports which button was pressed for which game.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_KIND_LABELS = {"install": "Installing", "update": "Updating", "repair": "Repairing"}


def downloads_label(active: int, paused: int) -> tuple[str, str]:
    """``(text, tooltip)`` for the Downloads entry: running and waiting, and paused apart (#941)."""
    if active and paused:
        return f"Downloads ({active} · {paused} paused)", (
            f"{active} downloading or waiting, {paused} paused"
        )
    if active:
        return f"Downloads ({active})", f"{active} downloading or waiting"
    if paused:
        return f"Downloads ({paused} paused)", f"{paused} paused"
    return "Downloads", ""


@dataclass
class DownloadRow:
    profile: str
    title: str
    operation: str  # install, update or repair
    state: str  # running, waiting or paused
    detail: str = ""  # running: the current step, in the app's own words
    fraction: float | None = None  # running: 0..1, or None while nothing is measurable
    eta: str = ""
    ahead: int = 0  # waiting: how many waiting installs come first
    can_pause: bool = False  # running or waiting: the download resumes from what is on disk
    can_now: bool = False  # waiting: the running install can be paused to start this one


_CANCEL_TIP = "Stop this download and take it off this list."
#: A download that resumes from disk keeps its files when it is cancelled (#991).
_CANCEL_KEEPS_TIP = (
    "Stop this download and take it off this list. What is already downloaded stays on disk, "
    "and the game page offers Resume."
)


def _button(text: str, tip: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("GhostButton")
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class DownloadRowWidget(QFrame):
    """One game's line: what it is doing and the buttons that make sense for its state."""

    clicked = pyqtSignal()

    def __init__(self, profile: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setObjectName("Card")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # Enter or Space opens the game page
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setStyleSheet("font-weight: 700; font-size: 14px;")
        # A long custom title is clipped (the full text is the tooltip), never the reason the
        # row grows past the window and pushes its buttons out of sight (#855).
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.title_label, 1)
        self.kind_label = QLabel()
        self.kind_label.setObjectName("Muted")
        head.addWidget(self.kind_label)
        head.addStretch(1)
        left.addLayout(head)
        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        # One line, clipped: a long file name must not make the row taller (the list would
        # jump under the pointer). The whole text stays in the tooltip.
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        left.addWidget(self.status_label)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        bar_row.addWidget(self.bar, 1)
        self.eta_label = QLabel()
        self.eta_label.setObjectName("Muted")
        bar_row.addWidget(self.eta_label)
        left.addLayout(bar_row)
        outer.addLayout(left, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.pause_btn = _button(
            "Pause", "Stop for now. Everything downloaded so far is kept, and Resume carries on."
        )
        self.resume_btn = _button("Resume", "Carry on from what is already downloaded.")
        self.next_btn = _button("Download next", "Move this game to the front of the line.")
        self.now_btn = _button(
            "Download now",
            "Pause the download that is running, start this one, and carry on with the other "
            "afterwards. Nothing already downloaded is lost.",
        )
        self.cancel_btn = _button("Cancel", _CANCEL_TIP)
        for button in (
            self.pause_btn,
            self.resume_btn,
            self.next_btn,
            self.now_btn,
            self.cancel_btn,
        ):
            buttons.addWidget(button)
        outer.addLayout(buttons)

    def apply(self, row: DownloadRow) -> None:
        self.title_label.setText(row.title)
        self.title_label.setToolTip(row.title)
        self.setAccessibleName(f"{row.title}: open the game page")
        self.kind_label.setText(_KIND_LABELS.get(row.operation, row.operation.capitalize()))
        running = row.state == "running"
        waiting = row.state == "waiting"
        paused = row.state == "paused"
        if running:
            self._set_status(row.detail or "Starting…")
        elif waiting:
            if row.ahead <= 0:
                self._set_status("Next in line")
            else:
                noun = "install" if row.ahead == 1 else "installs"
                self._set_status(f"Waiting · {row.ahead} {noun} ahead of this one")
        else:
            self._set_status("Paused. Everything downloaded so far is kept.")
        self.bar.setVisible(running)
        self.eta_label.setVisible(running)
        if running:
            self.set_progress(row.fraction, row.detail, row.eta)
        self.pause_btn.setVisible((running or waiting) and row.can_pause)
        self.resume_btn.setVisible(paused)
        self.next_btn.setVisible(waiting and row.ahead > 0)
        self.now_btn.setVisible(waiting and row.can_now)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setToolTip(_CANCEL_KEEPS_TIP if (row.can_pause or paused) else _CANCEL_TIP)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def set_progress(self, fraction: float | None, detail: str, eta: str) -> None:
        if fraction is None:
            self.bar.setRange(0, 0)  # busy: nothing measurable yet
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(max(0, min(100, int(round(fraction * 100)))))
        self.eta_label.setText(eta)
        if detail:
            self._set_status(detail)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:  # noqa: N802 - Qt naming
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:  # noqa: N802 - Qt naming
        if event is not None and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class DownloadsPage(QWidget):
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    next_requested = pyqtSignal(str)
    now_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, DownloadRowWidget] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 18)
        root.setSpacing(6)
        kicker = QLabel("DOWNLOADS")
        kicker.setObjectName("Section")
        root.addWidget(kicker)
        title = QLabel("Downloads")
        title.setObjectName("Title")
        root.addWidget(title)
        subtitle = QLabel(
            "Installs, updates and repairs that are running, waiting or paused. "
            "They run one at a time."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)
        root.addSpacing(10)

        self.empty_label = QLabel(
            "No downloads right now. An install or update shows up here while it runs."
        )
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        root.addWidget(self.empty_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self._list = QVBoxLayout(content)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(10)
        self._list.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def row_profiles(self) -> list[str]:
        return [self._list.itemAt(i).widget().profile for i in range(self._list.count() - 1)]

    def row(self, profile: str) -> DownloadRowWidget | None:
        return self._rows.get(profile)

    def set_rows(self, rows: list[DownloadRow]) -> None:
        wanted = {row.profile for row in rows}
        for profile in [name for name in self._rows if name not in wanted]:
            widget = self._rows.pop(profile)
            self._list.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        for row in rows:
            widget = self._rows.get(row.profile)
            if widget is None:
                widget = DownloadRowWidget(row.profile)
                self._wire(widget, row.profile)
                self._rows[row.profile] = widget
            widget.apply(row)
        # Put the (kept) widgets in the order given; the trailing stretch stays last.
        while self._list.count():
            self._list.takeAt(0)
        for row in rows:
            self._list.addWidget(self._rows[row.profile])
            self._rows[row.profile].show()
        self._list.addStretch(1)
        self.empty_label.setVisible(not rows)

    def _wire(self, widget: DownloadRowWidget, profile: str) -> None:
        widget.clicked.connect(lambda: self.open_requested.emit(profile))
        for button, signal in (
            (widget.pause_btn, self.pause_requested),
            (widget.resume_btn, self.resume_requested),
            (widget.cancel_btn, self.cancel_requested),
            (widget.next_btn, self.next_requested),
            (widget.now_btn, self.now_requested),
        ):
            button.clicked.connect(lambda _checked=False, s=signal: s.emit(profile))

    def update_progress(self, profile: str, fraction: float | None, detail: str, eta: str) -> None:
        widget = self._rows.get(profile)
        if widget is not None:
            widget.set_progress(fraction, detail, eta)
