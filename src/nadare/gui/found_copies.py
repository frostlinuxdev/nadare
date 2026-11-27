# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pick which copy of a game found on disk Nadare should use (#1293).

The start-up check lists working copies of a game whose remembered folder is gone. This
dialog only chooses one; :func:`nadare.core.gamefinder.use` re-points the profile and no
game file is moved or deleted.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
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

from ..core import diskspace
from ..core.gamefinder import Copy, Spare
from .dialog_layout import WrappedCheckBox, fit_dialog, make_scroll_body


def copy_text(copy: Copy, size: int | None = None) -> str:
    """One row's text: the folder, and on a second line what is known about it.

    The second line keeps "complete" visible however long the folder is (the path is elided
    in the middle, the full one is in the tooltip). ``size`` is added once measured (#1300).
    """
    facts = [f"version {copy.version}"] if copy.version else []
    facts.append("complete" if copy.complete else "incomplete")
    if size is not None:
        facts.append(diskspace.human_size(size))
    return f"{copy.game_dir}\n{' · '.join(facts)}"


class FoundCopiesDialog(QDialog):
    """List the copies best first; the first row starts selected."""

    #: (row, bytes) from the background measurement (#1300).
    size_measured = pyqtSignal(int, object)  # bytes: a Qt int is 32-bit (5 GiB read as 1 GiB)

    def __init__(
        self, title: str, copies: list[Copy], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title}: use a folder Nadare found")
        self.setMinimumWidth(620)
        self._shown_once = False
        self._copies = list(copies)
        #: ``Copy`` when the user picked one, ``"ignore"`` for Not now, else None.
        self.choice: Copy | str | None = None

        heading = QLabel(f"{title}: use a folder Nadare found")
        heading.setObjectName("Title")
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.PlainText)

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
        # Sizes are measured off the GUI thread once the dialog exists, never at start-up: a big
        # game is tens of thousands of files (#1300).
        self.size_measured.connect(self._size_ready)
        self._sizes = threading.Thread(
            target=self._measure, args=(list(self._copies),), name="copy-sizes", daemon=True
        )
        self._sizes.start()

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
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(heading)
        self._scroll, _body, body_layout = make_scroll_body(spacing=12)
        body_layout.addWidget(intro)
        body_layout.addWidget(self.list, 1)
        layout.addWidget(self._scroll, 1)
        layout.addLayout(buttons)
        self._fit_height()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height(keep_width=self._shown_once)
        self._shown_once = True

    def _fit_height(self, *, keep_width: bool = False) -> None:
        fit_dialog(self, min_width=620, scroll=self._scroll, keep_width=keep_width)

    def _measure(self, copies: list[Copy]) -> None:
        for row, copy in enumerate(copies):
            try:
                size = diskspace.tree_bytes(copy.game_dir)
            except OSError:
                continue
            try:
                self.size_measured.emit(row, size)
            except RuntimeError:
                return  # the dialog is gone

    def _size_ready(self, row: int, size: int) -> None:
        if not 0 <= row < len(self._copies):
            return
        item = self.list.item(row)
        if item is not None:
            item.setText(copy_text(self._copies[row], size))

    def wait_for_sizes(self, timeout: float | None = None) -> None:
        """For tests: wait until every copy was measured."""
        self._sizes.join(timeout)

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


def spare_text(spare: Spare) -> str:
    """One tick box: what the folder is, where, and its size."""
    what = "Game files" if spare.key == "install_dir" else "Wine prefix (saves and settings)"
    return f"{what}  ·  {diskspace.human_size(spare.size)}\n{spare.path}"


class SpareCopiesDialog(QDialog):
    """The folders of the copies not chosen (#1300 item 5): nothing ticked, Keep is the default."""

    def __init__(self, title: str, spares: list[Spare], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title}: other copies")
        self.setMinimumWidth(620)
        self._shown_once = False
        self._spares = list(spares)
        heading = QLabel(f"{title}: other copies")
        heading.setObjectName("Title")
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        intro = QLabel(
            f"{title} now runs from the folder you picked. Nadare also made these folders "
            "for other copies of it. Tick the ones to delete to free the space; a Wine prefix "
            "may hold saves or settings. This cannot be undone."
        )
        intro.setObjectName("Subtitle")
        intro.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(heading)
        self._scroll, _body, body_layout = make_scroll_body(spacing=12)
        body_layout.addWidget(intro)
        self.boxes: list[WrappedCheckBox] = []
        for spare in self._spares:
            box = WrappedCheckBox(spare_text(spare))
            box.setToolTip(str(spare.path))
            box.toggled.connect(self._update)
            body_layout.addWidget(box)
            self.boxes.append(box)
        layout.addWidget(self._scroll, 1)
        self.delete_button = QPushButton("Delete ticked folders")
        self.delete_button.setObjectName("GhostButton")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.accept)
        keep = QPushButton("Keep all")
        keep.setObjectName("Primary")
        keep.setDefault(True)
        keep.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(keep)
        layout.addLayout(buttons)
        self._fit_height()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height(keep_width=self._shown_once)
        self._shown_once = True

    def _fit_height(self, *, keep_width: bool = False) -> None:
        fit_dialog(self, min_width=620, scroll=self._scroll, keep_width=keep_width)

    def ticked(self) -> list[Spare]:
        return [s for s, box in zip(self._spares, self.boxes, strict=True) if box.isChecked()]

    def _update(self) -> None:
        self.delete_button.setEnabled(bool(self.ticked()))


__all__ = ["FoundCopiesDialog", "SpareCopiesDialog", "copy_text", "spare_text"]
