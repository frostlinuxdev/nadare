# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A thin strip above the pages for something the user should notice (a new release).

The text is always shown as plain text: it may come from outside the app.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget


class InfoBanner(QFrame):
    action_clicked = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 8, 12, 8)
        row.setSpacing(10)
        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        row.addWidget(self.message_label, 1)
        self.action_button = QPushButton()
        self.action_button.setObjectName("Primary")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(self.action_clicked)
        row.addWidget(self.action_button)
        self.dismiss_button = QPushButton("Later")
        self.dismiss_button.setObjectName("GhostButton")
        self.dismiss_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_button.clicked.connect(self.dismissed)
        row.addWidget(self.dismiss_button)
        self.setVisible(False)

    def show_message(self, text: str, action: str, *, dismissible: bool = True) -> None:
        self.message_label.setText(text)
        self.action_button.setText(action)
        self.action_button.setVisible(bool(action))
        self.dismiss_button.setVisible(dismissible)
        self.setVisible(True)

    def hide_banner(self) -> None:
        self.setVisible(False)


__all__ = ["InfoBanner"]
