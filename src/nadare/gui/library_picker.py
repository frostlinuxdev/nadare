# SPDX-License-Identifier: GPL-3.0-or-later
"""Pick a library for Move, or another folder when the library is not listed (#1293).

Move used to open a bare folder dialog. Picking a library's ``Nadare`` folder then made
``Nadare/Nadare/<Game>``. This dialog lists the libraries first; the free folder dialog is
still there behind ``Another folder…`` and :func:`resolve_other_folder` maps it the same way.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import libraries
from .dialog_layout import fit_dialog, make_scroll_body
from .libraries_panel import _free_text

#: The last row: open the ordinary folder dialog instead of using a listed library.
ANOTHER_FOLDER = "Another folder…"


def resolve_other_folder(path: str) -> Path:
    """The library a folder picked through "Another folder…" means, resolved.

    A picked ``Nadare`` folder means its parent library; any other folder is the library
    itself. Keeping this in one place stops Move from making ``Nadare/Nadare/<Game>``.
    """
    return Path(libraries.library_of(path)).resolve()


class LibraryPicker(QDialog):
    """A list of the user's libraries, the default first, plus "Another folder…"."""

    def __init__(
        self, config, title: str, parent: QWidget | None = None, *, include_default: bool = True
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._shown_once = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("Title")
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(heading)
        self._scroll, _body, body_layout = make_scroll_body(spacing=12)
        self.list = QListWidget()
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body_layout.addWidget(self.list, 1)
        layout.addWidget(self._scroll, 1)
        default = libraries.default_root()
        for root in libraries.library_roots(config):
            is_default = root == default
            if is_default and not include_default:
                continue  # Move / Change location: profiles/<name> cannot take a moved game
            if is_default:
                free = _free_text(root, must_exist=False)
                text = f"Default library  ({root})  · {free}"
                tooltip = f"Default library ({root}) · {free}"
            else:
                free = _free_text(root)
                text = f"{libraries.display_path(root)}  · {free}"
                tooltip = f"{root} · {free}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, root)
            item.setToolTip(tooltip)
            # Shown so the user sees where it was, but an unplugged library cannot be moved to.
            # Nadare's own library is made on demand, so its missing folder is not unplugged.
            if not is_default and not root.is_dir():
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(item)
        another = QListWidgetItem(ANOTHER_FOLDER)
        another.setData(Qt.ItemDataRole.UserRole, None)
        another.setToolTip(ANOTHER_FOLDER)
        self.list.addItem(another)
        first = next(
            (
                index
                for index in range(self.list.count())
                if self.list.item(index).flags() & Qt.ItemFlag.ItemIsEnabled
            ),
            0,
        )
        self.list.setCurrentRow(first)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fit_height()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height(keep_width=self._shown_once)
        self._shown_once = True

    def _fit_height(self, *, keep_width: bool = False) -> None:
        fit_dialog(self, min_width=520, scroll=self._scroll, keep_width=keep_width)

    def chosen(self) -> Path | None:
        """The picked library, or ``None`` for "Another folder…" (or no selection)."""
        item = self.list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return Path(value) if value is not None else None


__all__ = ["ANOTHER_FOLDER", "LibraryPicker", "resolve_other_folder"]
