# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The first-run account-safety notice modal.

The backend owns the notice text (:attr:`nadare.core.backends.GameBackend.account_notice`);
this dialog only presents it and collects the explicit acknowledgement.
"""

from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import i18n

#: Exact wording of the acknowledgement checkbox.
NOTICE_ACK_TEXT = "I have read this notice and understand the risks"


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


class HoldDialog(QDialog):
    """A signed hold list says this game is dangerous to start (#805)."""

    def __init__(self, hold, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hold = hold
        self.setWindowTitle(i18n.tr("Launch blocked"))
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        heading = QLabel(i18n.tr("Nadare has marked this game as dangerous to start"))
        heading.setObjectName("Heading")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.reason_label = QLabel(hold.reason)
        self.reason_label.setObjectName("Hint")
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reason_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.reason_label)

        if hold.link:
            report = escape(i18n.tr("Read the report"))
            self.link_label = QLabel(f'<a href="{escape(hold.link, quote=True)}">{report}</a>')
            self.link_label.setOpenExternalLinks(True)
            self.link_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
            layout.addWidget(self.link_label)
        else:
            self.link_label = QLabel("")
            self.link_label.hide()

        buttons = QDialogButtonBox()
        self.start_button = buttons.addButton(
            i18n.tr("I understand, start anyway"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.cancel_button = buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


__all__ = ["AccountNoticeDialog", "HoldDialog", "NOTICE_ACK_TEXT"]
