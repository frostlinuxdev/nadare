# SPDX-License-Identifier: GPL-3.0-or-later
"""The one confirm dialog behind the Store's Install button."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import libraries
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
        choice: Any = None,
        choice_sizes: Mapping[str, int] | None = None,
        on_choice: Callable[[str], None] | None = None,
        libraries: list[tuple[str, str]] | None = None,
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
        self.choice_combo: QComboBox | None = None
        self._on_choice = on_choice
        if choice is not None:
            self._add_choice(layout, choice, choice_sizes or {})
        if notice:
            head = QLabel("Nadare is an unofficial launcher")
            head.setStyleSheet("font-weight: 600;")
            layout.addWidget(head)
            layout.addWidget(self._label(notice))

        self.library_combo: QComboBox | None = None
        if libraries:
            # (label, folder) per library; the default library's folder is "".
            lib_row = QHBoxLayout()
            lib_row.addWidget(QLabel("Library"))
            combo = QComboBox()
            combo.setAccessibleName("Library")
            for label, folder in libraries:
                combo.addItem(label, folder)
                combo.setItemData(combo.count() - 1, folder or label, Qt.ItemDataRole.ToolTipRole)
            combo.currentIndexChanged.connect(self._library_changed)
            lib_row.addWidget(combo, 1)
            layout.addLayout(lib_row)
            self.library_combo = combo
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

    def _add_choice(self, layout: QVBoxLayout, option: Any, sizes: Mapping[str, int]) -> None:
        """A backend's install-time choice (WuWa's quality), each value with its size (#990)."""
        from ..core.diskspace import human_size

        row = QHBoxLayout()
        row.addWidget(QLabel(option.label))
        combo = QComboBox()
        for label, value in option.choices or ():
            size = sizes.get(value)
            combo.addItem(f"{label}, about {human_size(size)}" if size else label, value)
        combo.setAccessibleName(option.label)
        index = combo.findData(option.default)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(self._choice_changed)
        row.addWidget(combo)
        row.addStretch(1)
        layout.addLayout(row)
        if option.help:
            layout.addWidget(self._label(option.help))
        self.choice_combo = combo

    @property
    def choice_value(self) -> str | None:
        return None if self.choice_combo is None else self.choice_combo.currentData()

    def _choice_changed(self, _index: int) -> None:
        if self._on_choice is not None and self.choice_value is not None:
            self._on_choice(self.choice_value)
        try:
            self.folder_text, self._free, self.summary_text = self._describe(
                self.chosen_folder or None
            )
        except OSError:
            return
        self._show_folder()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow to the height the wrapped text needs at the width the dialog really has.

        The size hint is worked out at the layout's preferred width; shown narrower, the
        notice wraps to more lines and its first lines were cut off under the heading.
        """
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        need = layout.totalHeightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

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
        if self.isVisible():
            self._fit_height()

    def _want_import(self) -> None:
        self.import_requested = True
        self.reject()

    def change_location(self) -> None:
        start = self.chosen_folder or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if chosen:
            # A library's own "Nadare" folder means that library: select it, not a new entry.
            chosen = str(libraries.library_of(chosen))
        if not chosen or not self._use_folder(chosen):
            return
        combo = self.library_combo
        if combo is not None:
            index = combo.findData(chosen)
            if index < 0:
                combo.addItem(chosen, chosen)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _library_changed(self, _index: int) -> None:
        combo = self.library_combo
        if combo is None:
            return
        folder = combo.currentData() or ""
        if not self._use_folder(folder or None):
            combo.blockSignals(True)
            back = combo.findData(self.chosen_folder)
            combo.setCurrentIndex(back if back >= 0 else 0)
            combo.blockSignals(False)

    def _use_folder(self, chosen: str | None) -> bool:
        """Show what installing into ``chosen`` (``None``: the default library) means."""
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
            self._fit_height()
            return False
        self.chosen_folder = chosen or ""
        self.folder_text, self._free, self.summary_text = folder_text, free, summary
        self._show_folder()
        return True
