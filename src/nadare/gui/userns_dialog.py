# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A friendly dialog for Ubuntu's AppArmor user-namespace block (#429).

The raw fix is a wall of shell text. This dialog explains the problem in two
plain sentences, offers Ubuntu's normal password prompt through ``pkexec``, and
keeps the shell text behind "Show details" for anyone who prefers a terminal.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import userns

_COPIED = "Copied. Paste it into a terminal, then try again."
_WAITING = "Waiting for your password…"

#: What runs the blocked operation again once the permission is given (#1338: the block is now
#: caught before Play, so the dialog itself can start the game).
_RETRY_LABELS = {"launch": "Play now", "install": "Install now", "update": "Update now"}
_RETRY_DEFAULT = "Try again"
_DONE_NO_RETRY = "Done. Try again now."


class UsernsDialog(QDialog):
    """Explain the AppArmor block and offer a one-click fix."""

    fixed = pyqtSignal(bool, str)  # ok, message from the background fix

    def __init__(
        self, message: str, parent: QWidget | None = None, *, retry_op: str | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Permission needed")
        self.setMinimumWidth(560)
        self._message = message
        self._retry_op = retry_op
        #: True when the player pressed the retry button after a successful fix.
        self.retry_requested = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.summary = QLabel(userns.SHORT_MESSAGE + "\n\n" + userns.WITHOUT_IT)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFont("monospace"))
        head, details = userns.split_hint(message)
        self.details.setPlainText((f"{head}\n\n" if head else "") + (details or userns.hint()))
        self.details.hide()
        layout.addWidget(self.details)

        footer = QHBoxLayout()
        self.details_button = QPushButton("Show details")
        self.details_button.setObjectName("GhostButton")
        self.details_button.clicked.connect(self._toggle_details)
        footer.addWidget(self.details_button)
        footer.addStretch(1)

        self.copy_button = QPushButton("Copy fix command")
        self.copy_button.setObjectName("GhostButton")
        self.copy_button.clicked.connect(self._copy_command)
        footer.addWidget(self.copy_button)

        self.fix_button = QPushButton("Fix it")
        self.fix_button.setDefault(True)
        self.fix_button.clicked.connect(self._start_fix)
        self.fix_button.setVisible(userns.can_fix())
        footer.addWidget(self.fix_button)

        self.retry_button = QPushButton(_RETRY_LABELS.get(retry_op or "", _RETRY_DEFAULT))
        self.retry_button.clicked.connect(self._retry)
        self.retry_button.hide()
        footer.addWidget(self.retry_button)

        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("GhostButton")
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)

        layout.addLayout(footer)

        self.fixed.connect(self._on_fixed)

    def _toggle_details(self) -> None:
        visible = self.details.isHidden()
        self.details.setVisible(visible)
        self.details_button.setText("Hide details" if visible else "Show details")

    def _copy_command(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(userns.install_command())
        self.status.setText(_COPIED)

    def _start_fix(self) -> None:
        self.fix_button.setEnabled(False)
        self.status.setText(_WAITING)
        threading.Thread(target=self._run_fix, daemon=True).start()

    def _run_fix(self) -> None:
        try:
            ok, message = userns.install_profile()
        except Exception:  # noqa: BLE001 - a background thread must never escape
            ok = False
            message = (
                "Could not start the password prompt. Use Copy fix command instead."
            )
        try:
            self.fixed.emit(ok, message)
        except RuntimeError:
            return  # the dialog closed while the prompt was open

    def _on_fixed(self, ok: bool, message: str) -> None:
        if not ok:
            self.status.setText(message)
            self.fix_button.setEnabled(True)
            return
        self.fix_button.hide()
        self.copy_button.hide()
        if self._retry_op:
            self.status.setText(message)
            self.retry_button.show()
            self.retry_button.setDefault(True)
            self.retry_button.setFocus()
        else:
            self.status.setText(_DONE_NO_RETRY)

    def _retry(self) -> None:
        self.retry_requested = True
        self.accept()


__all__ = ["UsernsDialog"]
