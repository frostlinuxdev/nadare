# SPDX-License-Identifier: GPL-3.0-or-later
"""A modal sheet contained inside the main window while a game starts.

It has no close button and no Escape/click-outside exit: Stop is always there, and
Hide appears after :data:`HIDE_AFTER` so a missed window signal never traps the
player. It goes away by itself when the game's first frame is seen or the launch
ends, and at :data:`~nadare.core.launch_window.WATCH_SECONDS` its status says the
launch is still starting.
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..core.launch_window import WATCH_SECONDS
from . import i18n, theme

#: How long the player waits before the sheet offers Hide. Stop is always there; this
#: only stops a missed window signal from keeping the sheet up for the whole timeout.
HIDE_AFTER = 10.0


class LaunchSheet(QWidget):
    """The in-window launch sheet; the main window owns one at a time."""

    stopped = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.started = time.monotonic()
        self.running_at = None
        self.timed_out = False
        self._finished = False
        self.setObjectName("launchSheet")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#launchSheet {{ background: rgba(7, 9, 13, 235); }}"
            f"#launchCard {{ background: {theme.PANEL_2}; border: 1px solid {theme.BORDER};"
            f" border-radius: 16px; }}"
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.addStretch()
        card = QFrame(self)
        card.setObjectName("launchCard")
        card.setMaximumWidth(480)
        card.setMinimumWidth(360)
        content = QVBoxLayout(card)
        content.setContentsMargins(30, 28, 30, 26)
        content.setSpacing(14)

        self.title = QLabel(title)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 25px; font-weight: 800;")
        self.status = QLabel(i18n.tr("Preparing"))
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_SOFT}; font-size: 14px;")
        self.elapsed = QLabel(i18n.tr("{seconds} s", seconds=0))
        self.elapsed.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        content.addWidget(self.title)
        content.addWidget(self.status)
        content.addWidget(self.elapsed)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.stop = QPushButton(i18n.tr("Stop"))
        self.stop.setObjectName("Primary")
        self.stop.setMinimumWidth(140)
        self.stop.setMinimumHeight(48)
        self.stop.clicked.connect(self._stop)
        self.hide_button = QPushButton(i18n.tr("Hide"))
        self.hide_button.setObjectName("GhostButton")
        self.hide_button.hide()
        self.hide_button.clicked.connect(self.finish)
        buttons.addWidget(self.stop)
        buttons.addWidget(self.hide_button)
        buttons.addStretch()
        content.addLayout(buttons)

        layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()

        self._central = parent.centralWidget()
        self._central_enabled = self._central.isEnabled()
        self._central.setEnabled(False)
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.show()
        self.raise_()
        self.stop.setFocus()

    def buttons(self) -> list[QPushButton]:
        """The buttons a user can press now (Hide appears after :data:`HIDE_AFTER`)."""
        return [self.stop] + ([self.hide_button] if not self.hide_button.isHidden() else [])

    def eventFilter(self, obj, event):
        if obj is self.parent():
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(obj.rect())
            elif event.type() == QEvent.Type.Hide:
                self.finish()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        event.ignore()

    def keyPressEvent(self, event):
        # Escape, Enter and every other key stay inside: none of them dismisses the sheet.
        event.accept()

    def mousePressEvent(self, event):
        # A click outside the card is absorbed by the sheet, never a way out.
        event.accept()

    def progress(self, event) -> None:
        """Apply a launch event's status line (the window signal ends the sheet instead)."""
        if self._finished or self.timed_out:
            return
        if event.kind == "game_running":
            self.running_at = time.monotonic()
            self.status.setText(i18n.tr("Waiting for the window"))
        elif (
            self.running_at is None
            and event.kind in ("step", "log", "status")
            and any(word in event.message.lower() for word in ("launching ", "starting "))
        ):
            self.status.setText(i18n.tr("Starting Wine"))

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self.started)
        self.elapsed.setText(i18n.tr("{seconds} s", seconds=int(elapsed)))
        if elapsed >= HIDE_AFTER:
            self.hide_button.show()
        if elapsed >= WATCH_SECONDS:
            self.timed_out = True
            self.status.setText(
                i18n.tr("Still starting. Its own launcher or anti-cheat can take a while.")
            )
        elif self.running_at is not None and now - self.running_at >= 1:
            self.status.setText(i18n.tr("Waiting for the window"))

    def _stop(self) -> None:
        self.stopped.emit()
        self.finish()

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.timer.stop()
        self.hide()
        self.parent().removeEventFilter(self)
        self._central.setEnabled(self._central_enabled)
        self.finished.emit()
