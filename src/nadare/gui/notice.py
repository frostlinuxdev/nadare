# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The first-run account-safety notice modal.

The backend owns the notice text (:attr:`nadare.core.backends.GameBackend.account_notice`);
this dialog only presents it and collects the explicit acknowledgement.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

#: Exact wording of the acknowledgement checkbox.
NOTICE_ACK_TEXT = "I have secured my account (linked it or saved my transfer number)"


class AccountNoticeDialog(QDialog):
    """Modal notice that blocks install/launch until it is acknowledged."""

    def __init__(self, notice: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Account notice")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        self.notice_label = QLabel(notice)
        self.notice_label.setObjectName("Hint")
        self.notice_label.setWordWrap(True)
        # The backend supplies this text: never interpret it as rich text.
        self.notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.notice_label)

        self.checkbox = QCheckBox(NOTICE_ACK_TEXT)
        self.checkbox.toggled.connect(self._on_toggled)
        layout.addWidget(self.checkbox)

        buttons = QDialogButtonBox()
        self.continue_button = buttons.addButton(
            "Continue", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.continue_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_toggled(self, checked: bool) -> None:
        self.continue_button.setEnabled(checked)


__all__ = ["AccountNoticeDialog", "NOTICE_ACK_TEXT"]
