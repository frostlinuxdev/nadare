# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The in-app log viewer: pick a log, watch the latest run, search and copy."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import diskspace, logview, paths


def _human_size(size: int) -> str:
    return diskspace.human_size(size)


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


class LogsDialog(QDialog):
    """Browse Nadare's app log and per-game operation logs."""

    #: Ask for the one-button support report (distro, GPU, doctor, profile, recent logs).
    report_requested = pyqtSignal(str)

    def __init__(
        self,
        profiles: Iterable[str],
        profile: str | None = None,
        operation: str | None = None,
        directory: Path | str | None = None,
        follow: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.setMinimumSize(900, 560)
        self._profiles = list(profiles)
        self._profile = profile
        self._operation = operation
        self._directory = Path(directory) if directory is not None else paths.logs_dir()
        self._file_text = ""
        self._raw_text = ""
        self._stat: tuple[int, int] | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        heading = QLabel("Logs")
        heading.setObjectName("Title")
        layout.addWidget(heading)
        note = QLabel(
            "Nadare's own log and the logs from each game operation. Copy or save a "
            "run to include it in a bug report; the redaction box hides your username "
            "and file paths first."
        )
        note.setObjectName("Subtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        content = QHBoxLayout()
        content.setSpacing(12)
        self.files = QListWidget()
        self.files.setObjectName("LogsList")
        self.files.setMinimumWidth(270)
        self.files.setAlternatingRowColors(True)
        content.addWidget(self.files, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        options = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter lines…")
        self.search.setClearButtonEnabled(True)
        options.addWidget(self.search, 1)
        self.latest_only = QCheckBox("Latest run only")
        self.latest_only.setChecked(True)
        options.addWidget(self.latest_only)
        self.redact_box = QCheckBox("Hide my username and paths")
        self.redact_box.setChecked(True)
        options.addWidget(self.redact_box)
        right.addLayout(options)

        self.view = QPlainTextEdit()
        self.view.setObjectName("LogsView")
        self.view.setObjectName("Console")
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(200_000)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        right.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        self.copy = QPushButton("Copy")
        self.copy.setObjectName("GhostButton")
        self.copy.clicked.connect(self._copy)
        buttons.addWidget(self.copy)
        self.save = QPushButton("Save as…")
        self.save.setObjectName("GhostButton")
        self.save.clicked.connect(self._save_as)
        buttons.addWidget(self.save)
        self.zip_btn = QPushButton("Save all as zip…")
        self.zip_btn.setObjectName("GhostButton")
        self.zip_btn.setToolTip(
            "Every log listed here in one zip to attach to a bug report; the redaction "
            "box decides whether your username and paths are hidden."
        )
        self.zip_btn.clicked.connect(self._save_zip)
        buttons.addWidget(self.zip_btn)
        self.folder = QPushButton("Open folder")
        self.folder.setObjectName("GhostButton")
        self.folder.clicked.connect(self._open_folder)
        buttons.addWidget(self.folder)
        self.report_btn = QPushButton("Get support report")
        self.report_btn.setObjectName("GhostButton")
        self.report_btn.setToolTip(
            "Distro, GPU, doctor checks, this game's settings and its recent logs in one "
            "redacted text, to copy or save. Nothing is uploaded."
        )
        self.report_btn.clicked.connect(self._request_report)
        buttons.addWidget(self.report_btn)
        buttons.addStretch(1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("GhostButton")
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        right.addLayout(buttons)
        content.addLayout(right, 3)
        layout.addLayout(content, 1)

        self.files.currentRowChanged.connect(self._load_selected)
        self.search.textChanged.connect(self._render)
        self.latest_only.toggled.connect(self._render)
        self.redact_box.toggled.connect(self._render)

        self._reload_files()
        self.zip_btn.setEnabled(self.files.count() > 0)
        self._load_selected()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        if follow:
            self._timer.start()

    # ---- file list -----------------------------------------------------------
    def _selected_path(self) -> Path | None:
        item = self.files.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return Path(value) if value else None

    def _reload_files(self) -> None:
        """Rebuild the list, keeping the current file selected when possible."""
        current = self._selected_path()
        files = logview.list_logs(
            self._profiles, self._directory, profile=self._profile
        )
        self.files.blockSignals(True)
        self.files.clear()
        for log in files:
            item_text = f"{log.label}   {_human_size(log.size)}  {_when(log.mtime)}"
            self.files.addItem(item_text)
            item = self.files.item(self.files.count() - 1)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, str(log.path))
                item.setToolTip(str(log.path))
        target = -1
        if current is not None:
            for row in range(self.files.count()):
                item = self.files.item(row)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == str(current):
                    target = row
                    break
        if target < 0:
            for row, log in enumerate(files):
                if (
                    self._operation is not None
                    and log.operation == self._operation
                    and (self._profile is None or log.profile == self._profile)
                ):
                    target = row
                    break
        if target < 0 and self.files.count():
            target = 0
        self.files.setCurrentRow(target)
        self.files.blockSignals(False)
        if hasattr(self, "zip_btn"):
            self.zip_btn.setEnabled(bool(files))

    # ---- content -------------------------------------------------------------
    def _load_selected(self, _row: int = -1) -> None:
        path = self._selected_path()
        if path is None:
            self._file_text = ""
            self._stat = None
            self._render()
            return
        self._file_text = logview.read_tail(path)
        try:
            stat = path.stat()
            self._stat = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            self._stat = None
        self._render()

    def _render(self, *_args: object) -> None:
        text = self._file_text
        if self.latest_only.isChecked():
            parts = logview.sessions(text)
            text = parts[-1] if parts else ""
        text = logview.filter_lines(text, self.search.text())
        self._raw_text = text
        shown = logview.redacted(text) if self.redact_box.isChecked() else text
        self.view.setPlainText(shown)

    def current_text(self) -> str:
        """The text currently displayed, redacted when the box is checked."""
        if self.redact_box.isChecked():
            return logview.redacted(self._raw_text)
        return self._raw_text

    def refresh(self) -> None:
        """Re-read the selected file now, picking up anything appended."""
        self._reload_files()
        self._load_selected()

    # ---- buttons -------------------------------------------------------------
    def _request_report(self) -> None:
        name = self._profile or (self._profiles[0] if self._profiles else "")
        self.report_requested.emit(name)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.current_text())

    def _save_as(self) -> None:
        path = self._selected_path()
        suggested = str(path) if path is not None else str(self._directory / "nadare.log")
        name, _filter = QFileDialog.getSaveFileName(
            self, "Save log", suggested, "Log files (*.log *.txt);;All files (*)"
        )
        if not name:
            return
        Path(name).write_text(self.current_text(), encoding="utf-8")

    def _save_zip(self) -> None:
        suggested = str(Path.home() / f"nadare-logs-{datetime.now():%Y%m%d-%H%M}.zip")
        name, _filter = QFileDialog.getSaveFileName(
            self, "Save logs as zip", suggested, "Zip files (*.zip);;All files (*)"
        )
        if not name:
            return
        try:
            logview.bundle(
                self._profiles,
                self._directory,
                name,
                profile=self._profile,
                redact=self.redact_box.isChecked(),
            )
        except OSError as exc:
            QMessageBox.warning(self, "Save logs as zip", f"Could not save the zip: {exc}")

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._directory)))

    # ---- follow mode ---------------------------------------------------------
    def _tick(self) -> None:
        path = self._selected_path()
        if path is None:
            self.refresh()
            return
        try:
            stat = path.stat()
        except OSError:
            self.refresh()
            return
        if self._stat == (stat.st_size, stat.st_mtime_ns):
            return
        scrollbar = self.view.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self._load_selected()
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())


__all__ = ["LogsDialog"]
