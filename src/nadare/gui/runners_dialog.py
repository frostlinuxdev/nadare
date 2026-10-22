# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Manage the Proton runners Nadare has downloaded: see them, delete the unused ones."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import runners

COLUMNS = ("Runner", "Status", "Size", "Used by", "")


def human_size(size: int) -> str:
    """``1.6 GB`` style size."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def status_text(path: Path) -> str:
    if runners.is_recommended(path):
        return "recommended (tested)"
    if runners.runner_family(path):
        return "untested"
    return "custom"


class _Worker(QObject):
    """Usage, sizes and deletions run off the GUI thread; results come back as signals."""

    usage_ready = pyqtSignal(int, object, object)  # generation, usage, unused
    size_ready = pyqtSignal(str, int)
    deleted = pyqtSignal(str, bool)


class ManageRunnersDialog(QDialog):
    """Lists the runners in Nadare's own folder and deletes the ones no profile uses."""

    def __init__(
        self,
        profiles: Mapping[str, Mapping[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage runners")
        self.setMinimumWidth(640)
        self._profiles = profiles
        self._worker = _Worker(self)
        self._worker.usage_ready.connect(self._on_usage)
        self._worker.size_ready.connect(self._on_size)
        self._worker.deleted.connect(self._on_deleted)
        self._rows: dict[str, int] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._usage: dict[str, list[str]] = {}
        self._usage_ready = False
        self._unused: list[Path] = []
        self._generation = 0
        self._missing: list[Path] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        heading = QLabel("Proton runners downloaded by Nadare")
        heading.setObjectName("Title")
        layout.addWidget(heading)
        note = QLabel(
            "The recommended build is the one Nadare has tested. A runner that a game "
            "uses cannot be deleted; one that is not used is safe to remove, and the "
            "recommended build is downloaded again the next time a game needs it."
        )
        note.setObjectName("Subtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        self.empty_label = QLabel("No runners downloaded by Nadare yet.")
        self.empty_label.setObjectName("Muted")
        layout.addWidget(self.empty_label)

        self.total_label = QLabel("")
        self.total_label.setObjectName("Muted")
        layout.addWidget(self.total_label)

        footer = QHBoxLayout()
        self.cleanup_button = QPushButton("Delete unused old runners")
        self.cleanup_button.setObjectName("GhostButton")
        self.cleanup_button.setToolTip(
            "Removes every runner no game uses, except the recommended build."
        )
        self.cleanup_button.clicked.connect(self._confirm_delete_unused)
        footer.addWidget(self.cleanup_button)
        footer.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

        self._sizes: dict[str, int] = {}
        self.reload()

    # ---- content -------------------------------------------------------------
    def reload(self) -> None:
        """Rebuild the table from disk, then refresh usage in the background."""
        self._generation += 1
        generation = self._generation
        self._usage_ready = False
        self._unused = []
        installed = runners.nadare_runners()
        self.table.setRowCount(0)
        self._rows.clear()
        self._buttons.clear()
        for path in installed:
            row = self.table.rowCount()
            self.table.insertRow(row)
            key = str(path)
            self._rows[key] = row
            users = self._usage.get(key, [])
            self.table.setItem(row, 0, QTableWidgetItem(path.name))
            self.table.setItem(row, 1, QTableWidgetItem(status_text(path)))
            size = self._sizes.get(key)
            self.table.setItem(
                row, 2, QTableWidgetItem(human_size(size) if size is not None else "…")
            )
            self.table.setItem(row, 3, QTableWidgetItem(", ".join(users) if users else "—"))
            button = QPushButton("Delete")
            button.setObjectName("GhostButton")
            button.setMinimumWidth(96)
            if users:
                button.setEnabled(False)
                button.setToolTip("In use by: " + ", ".join(users))
            else:
                # Do not offer a delete until the background check says it is safe.
                button.setEnabled(False)
                button.setToolTip("Checking which games use this runner…")
            button.clicked.connect(lambda _checked=False, p=path: self._confirm_delete(p))
            self.table.setCellWidget(row, 4, button)
            self._buttons[key] = button
        self.cleanup_button.setEnabled(False)
        self.cleanup_button.setText("Checking runners…")
        self.empty_label.setVisible(not installed)
        self.table.setVisible(bool(installed))
        self._update_total()
        self._missing = [p for p in installed if str(p) not in self._sizes]
        profiles = dict(self._profiles)
        threading.Thread(
            target=self._measure_usage, args=(generation, profiles), daemon=True
        ).start()

    def _measure_usage(self, generation: int, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        usage = runners.runner_usage(profiles)
        unused = sorted(
            Path(key)
            for key, users in usage.items()
            if not users and not runners.is_recommended(key)
        )
        try:
            self._worker.usage_ready.emit(generation, usage, unused)
        except RuntimeError:
            return  # the dialog closed while we were checking

    def _on_usage(self, generation: int, usage: object, unused: object) -> None:
        if generation != self._generation:
            return  # a newer reload already started
        self._usage = dict(usage) if isinstance(usage, dict) else {}
        self._unused = list(unused) if isinstance(unused, list) else []
        self._usage_ready = True
        self._apply_usage()
        if self._missing:
            missing, self._missing = self._missing, []
            threading.Thread(target=self._measure, args=(missing,), daemon=True).start()

    def _apply_usage(self) -> None:
        for key, row in self._rows.items():
            users = self._usage.get(key, [])
            self.table.setItem(row, 3, QTableWidgetItem(", ".join(users) if users else "—"))
            button = self._buttons.get(key)
            if button is None:
                continue
            button.setEnabled(not users)
            button.setToolTip("In use by: " + ", ".join(users) if users else "")
        self.cleanup_button.setEnabled(bool(self._unused))
        self.cleanup_button.setText(
            f"Delete unused old runners ({len(self._unused)})"
            if self._unused
            else "Delete unused old runners"
        )

    def _ensure_usage(self) -> None:
        """A direct delete before the background check finished gets a safe answer."""
        if self._usage_ready:
            return
        self._usage = runners.runner_usage(self._profiles)
        self._unused = sorted(
            Path(key)
            for key, users in self._usage.items()
            if not users and not runners.is_recommended(key)
        )
        self._usage_ready = True
        self._apply_usage()

    def _measure(self, paths_in: list[Path]) -> None:
        for path in paths_in:
            try:
                self._worker.size_ready.emit(str(path), runners.runner_size(path))
            except RuntimeError:
                return  # the dialog closed while we were measuring

    def _on_size(self, key: str, size: int) -> None:
        self._sizes[key] = size
        row = self._rows.get(key)
        if row is not None:
            self.table.setItem(row, 2, QTableWidgetItem(human_size(size)))
        self._update_total()

    def _update_total(self) -> None:
        if self._sizes:
            self.total_label.setText(f"Total: {human_size(sum(self._sizes.values()))}")
        else:
            self.total_label.setText("")

    # ---- deleting ------------------------------------------------------------
    def _confirm_delete(self, path: Path) -> None:
        size = self._sizes.get(str(path))
        extra = ""
        if runners.is_recommended(path):
            extra = "\n\nThis is the recommended build; it will be downloaded again when needed."
        answer = QMessageBox.question(
            self,
            "Delete runner",
            f"Delete {path.name}"
            + (f" ({human_size(size)})" if size is not None else "")
            + f"?{extra}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # a destructive action never defaults to Yes
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_delete(path)

    def _confirm_delete_unused(self) -> None:
        if not self._unused:
            return
        names = "\n".join(f"  • {p.name}" for p in self._unused)
        known = [self._sizes[str(p)] for p in self._unused if str(p) in self._sizes]
        size = f" (about {human_size(sum(known))})" if len(known) == len(self._unused) else ""
        answer = QMessageBox.question(
            self,
            "Delete unused runners",
            f"Delete these runners{size}? No game uses them.\n\n{names}"
            "\n\nThe recommended build is kept. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_delete_many(list(self._unused))

    def start_delete_many(self, paths_in: list[Path]) -> None:
        """Delete several unused runners in the background (skips any now in use)."""
        self._ensure_usage()
        todo = [p for p in paths_in if not self._usage.get(str(p))]
        if not todo:
            return
        self.cleanup_button.setEnabled(False)
        self.cleanup_button.setText("Deleting…")
        for button in self._buttons.values():  # no overlapping deletes from the rows
            button.setEnabled(False)
        threading.Thread(target=self._delete_all, args=(todo,), daemon=True).start()

    def _delete_all(self, todo: list[Path]) -> None:
        for path in todo:
            self._delete(path)

    def start_delete(self, path: Path) -> None:
        """Delete ``path`` in the background; refuses a runner a profile uses."""
        self._ensure_usage()
        if self._usage.get(str(path)):
            QMessageBox.information(
                self, "Delete runner", "In use by: " + ", ".join(self._usage[str(path)])
            )
            return
        button = self._buttons.get(str(path))
        if button is not None:
            button.setEnabled(False)
            button.setText("Deleting…")
        threading.Thread(target=self._delete, args=(path,), daemon=True).start()

    def _delete(self, path: Path) -> None:
        try:
            ok = runners.remove(path)
        except OSError:
            ok = False
        try:
            self._worker.deleted.emit(str(path), ok)
        except RuntimeError:
            return

    def _on_deleted(self, key: str, ok: bool) -> None:
        self._sizes.pop(key, None)
        if not ok:
            QMessageBox.warning(self, "Delete runner", f"Could not delete {Path(key).name}.")
        self.reload()


__all__ = ["COLUMNS", "ManageRunnersDialog", "human_size", "status_text"]
