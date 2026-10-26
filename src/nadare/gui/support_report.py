# SPDX-License-Identifier: GPL-3.0-or-later
"""Report preview and background report generation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class _Signals(QObject):
    finished = pyqtSignal(str)


class ReportTask(QRunnable):
    def __init__(self, config: dict, profile: str) -> None:
        super().__init__()
        self.config = config
        self.profile = profile
        self.signals = _Signals()

    def run(self) -> None:
        from ..app import _doctor_text
        from ..core.config import Config
        from ..core.report import build_report

        text = build_report(
            self.config,
            profile_name=self.profile,
            doctor_text=_doctor_text(Config(data=self.config)),
        )
        self.signals.finished.emit(text)


class ReportDialog(QDialog):
    def __init__(self, parent, profile: str) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle("Report a problem")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Nothing is uploaded. Check the text, then paste it where you are getting help.")
        )
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText("Building report…")
        layout.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy to clipboard")
        self.copy_button.clicked.connect(self._copy)
        buttons.addWidget(self.copy_button)
        self.save_button = QPushButton("Save as file…")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def set_report(self, text: str) -> None:
        self.text.setPlainText(text)

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.text.toPlainText())

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save report",
            str(Path.home() / f"nadare-report-{self.profile}-{date.today().isoformat()}.txt"),
            "Text files (*.txt)",
        )
        if not path:
            return
        try:
            target = Path(path)
            target.write_text(self.text.toPlainText(), encoding="utf-8")
            target.chmod(0o600)
        except OSError as exc:
            QMessageBox.critical(self, "Save report", f"Could not save report: {exc}")
