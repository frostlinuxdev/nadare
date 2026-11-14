# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pick which copy of a game found on disk Nadare should use (#1293).

The start-up check lists working copies of a game whose remembered folder is gone. This
dialog only chooses one; :func:`nadare.core.gamefinder.use` re-points the profile and no
game file is moved or deleted.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.gamefinder import Copy


def copy_text(copy: Copy) -> str:
    """One row's text: the folder, and on a second line what is known about it.

    The second line keeps "complete" visible however long the folder is (the path is elided
    in the middle, the full one is in the tooltip).
    """
    facts = [f"version {copy.version}"] if copy.version else []
    facts.append("complete" if copy.complete else "incomplete")
    return f"{copy.game_dir}\n{' · '.join(facts)}"


class FoundCopiesDialog(QDialog):
    """List the copies best first; the first row starts selected."""

    def __init__(
        self, title: str, copies: list[Copy], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title}: use a folder Nadare found")
        self.setMinimumWidth(620)
        self._copies = list(copies)
        #: ``Copy`` when the user picked one, ``"ignore"`` for Not now, else None.
        self.choice: Copy | str | None = None

        intro = QLabel(
            f"Nadare found {title} in these folders. Pick the one to use; "
            "nothing is moved or deleted."
        )
        intro.setObjectName("Subtitle")
        intro.setWordWrap(True)

        self.list = QListWidget()
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for copy in self._copies:
            item = QListWidgetItem(copy_text(copy))
            item.setToolTip(str(copy.game_dir))
            self.list.addItem(item)
        if self._copies:
            self.list.setCurrentRow(0)

        use = QPushButton("Use this folder")
        use.setObjectName("Primary")
        not_now = QPushButton("Not now")
        not_now.setObjectName("GhostButton")
        cancel = QPushButton("Cancel")
        cancel.setObjectName("GhostButton")
        use.clicked.connect(self._use)
        not_now.clicked.connect(self._not_now)
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(not_now)
        buttons.addWidget(use)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

    def chosen(self) -> Copy | None:
        """The selected copy, or None when the list is empty."""
        row = self.list.currentRow()
        return self._copies[row] if 0 <= row < len(self._copies) else None

    def _use(self) -> None:
        self.choice = self.chosen()
        self.accept()

    def _not_now(self) -> None:
        self.choice = "ignore"
        self.accept()


__all__ = ["FoundCopiesDialog", "copy_text"]
