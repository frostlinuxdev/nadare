# SPDX-License-Identifier: GPL-3.0-or-later
"""The one confirm dialog behind the Store's Install button."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import apply_theme


def _elide_middle(text: str, limit: int = 64) -> str:
    """``text`` cut in the middle with an ellipsis once it is longer than ``limit``."""
    if len(text) <= limit:
        return text
    head = limit // 2 - 3
    return f"{text[:head]}…{text[-(limit - head - 1):]}"


class QuickInstallDialog(QDialog):
    """Size, free space, the unofficial-launcher notice and the install folder, once.

    ``describe(chosen)`` returns ``(folder, free, summary)`` for the folder the user picked
    (``None`` = the default), so what is shown is where the game will really go.
    ``summary_text`` and ``notice_text`` are empty when there is nothing to say;
    ``import_requested`` is set when the user wants to use files they already have.
    """

    def __init__(
        self,
        title: str,
        *,
        notice: str,
        describe: Callable[[str | None], tuple[str, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        apply_theme(self)
        self.setWindowTitle(f"Install {title}")
        self.setMinimumWidth(520)
        self._describe = describe
        self.notice_text = notice
        self.chosen_folder = ""
        self.import_requested = False
        self.folder_text, self._free, self.summary_text = describe(None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(f"Install {title}?")
        heading.setObjectName("Title")
        layout.addWidget(heading)

        self.summary_label = self._label("")
        layout.addWidget(self.summary_label)
        if notice:
            head = QLabel("Nadare is an unofficial launcher")
            head.setStyleSheet("font-weight: 600;")
            layout.addWidget(head)
            layout.addWidget(self._label(notice))

        self.folder_label = self._label("")
        layout.addWidget(self.folder_label)
        row = QHBoxLayout()
        self.change_button = QPushButton("Change install location…")
        self.change_button.setAutoDefault(False)
        self.change_button.clicked.connect(self.change_location)
        row.addWidget(self.change_button)
        self.import_button = QPushButton("I already have it installed…")
        self.import_button.setAutoDefault(False)
        self.import_button.clicked.connect(self._want_import)
        row.addWidget(self.import_button)
        row.addStretch(1)
        layout.addLayout(row)
        self._show_folder()

        buttons = QDialogButtonBox()
        self.install_button = buttons.addButton(
            "I understand, install" if notice else "Install",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.install_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("Hint")
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def _show_folder(self) -> None:
        extra = f" ({self._free})" if self._free else ""
        shown = _elide_middle(self.folder_text)
        self.folder_label.setText(f"Install folder: {shown}{extra}")
        # A path has no spaces to wrap at, so a long one would make the dialog very wide.
        self.folder_label.setToolTip(self.folder_text if shown != self.folder_text else "")
        self.summary_label.setText(self.summary_text)
        self.summary_label.setVisible(bool(self.summary_text))

    def _want_import(self) -> None:
        self.import_requested = True
        self.reject()

    def change_location(self) -> None:
        start = self.chosen_folder or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if not chosen:
            return
        try:
            folder_text, free, summary = self._describe(chosen)
        except OSError as exc:
            # A folder we may not look into: keep the one in use, and put the page's state back
            # (describe records the pick before it fails).
            try:
                self._describe(self.chosen_folder or None)
            except OSError:
                pass
            reason = exc.strerror or str(exc)
            self.summary_label.setText(
                f"Nadare cannot use {chosen} ({reason}). Pick another folder."
            )
            self.summary_label.setVisible(True)
            return
        self.chosen_folder = chosen
        self.folder_text, self._free, self.summary_text = folder_text, free, summary
        self._show_folder()
