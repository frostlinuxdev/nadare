# SPDX-License-Identifier: GPL-3.0-or-later
"""The System check window: ``nadare doctor`` for people who never open a terminal (#668).

It runs the same check as the command line, on a worker thread, and shows the text.
Nothing is changed and nothing is uploaded; the person copies the result where they need it.
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .theme import DIM, MUTED, TEXT, TEXT_SOFT, WARN

log = logging.getLogger(__name__)


#: Words that make a line worth a second look. A flag is not a verdict: the note under a line
#: says whether it matters.
_FLAG_WORDS = (
    "missing",
    "not found",
    "unavailable",
    "none found",
    "failed",
    "could not",
    "error",
    "software-only",
    "no hardware",
)
_LABEL_MAX_CHARS = 24
_LABEL_MAX_WORDS = 3


@dataclass(frozen=True)
class ReportLine:
    kind: str  # "item" (label and body), "heading", "note" (indented) or "plain"
    label: str
    body: str
    warn: bool


def parse_report(text: str) -> list[ReportLine]:
    """Split the doctor text into lines a person can scan; the words are never changed."""
    lines: list[ReportLine] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[:1] in (" ", "\t"):
            lines.append(ReportLine("note", "", raw.strip(), False))
            continue
        stripped = raw.strip()
        flagged = any(word in stripped.lower() for word in _FLAG_WORDS)
        label, sep, body = stripped.partition(": ")
        short = len(label) <= _LABEL_MAX_CHARS and len(label.split()) <= _LABEL_MAX_WORDS
        if sep and short and body:
            lines.append(ReportLine("item", label, body.strip(), flagged))
        elif stripped.endswith(":") and len(stripped) - 1 <= _LABEL_MAX_CHARS:
            lines.append(ReportLine("heading", stripped[:-1], "", False))
        else:
            lines.append(ReportLine("plain", "", stripped, flagged))
    return lines


def _escaped(text: str) -> str:
    return html.escape(text, quote=False)


def _note_html(text: str) -> str:
    """A note, with the command to copy (after ``sudo``) set in a monospace face."""
    match = re.search(r"\bsudo\b", text)
    if match is None:
        return _escaped(text)
    head, command = text[: match.start()], text[match.start() :]
    return (
        f"{_escaped(head)}<code style='font-family:monospace;color:{TEXT_SOFT}'>"
        f"{_escaped(command)}</code>"
    )


def report_html(text: str) -> str:
    """The report as a small table: marker, label and body, with a one-line summary on top."""
    lines = parse_report(text)
    verdict = next((line for line in lines if line.label == "verdict"), None)
    if verdict is not None:
        # doctor's own answer (#1002): a ▲ below is a detail unless the verdict names it.
        lines = [line for line in lines if line is not verdict]
        ready = verdict.body.startswith("ready")
        colour = TEXT if ready else WARN
        words = verdict.body.split(" (", 1)[0]
        words = words[:1].upper() + words[1:]
        hint = (
            "A ▲ below is a detail, not a problem."
            if ready
            else "The fix is under the flagged line below."
        )
        summary = (
            f"<p style='margin:0 0 10px 0'><b style='color:{colour}'>"
            f"{'✓' if ready else '▲'} {_escaped(words)}.</b>"
            f" <span style='color:{MUTED}'>{hint}</span></p>"
        )
        return summary + _report_table(lines)
    flagged = sum(1 for line in lines if line.warn)
    if flagged:
        noun = "item" if flagged == 1 else "items"
        summary = (
            f"<p style='margin:0 0 10px 0'><b style='color:{WARN}'>▲ {flagged} {noun} flagged.</b>"
            f" <span style='color:{MUTED}'>A flag is not always a problem: the note under it says"
            " what it affects.</span></p>"
        )
    else:
        summary = (
            f"<p style='margin:0 0 10px 0'><b style='color:{TEXT}'>Nothing flagged.</b></p>"
        )
    return summary + _report_table(lines)


def _report_table(lines: list[ReportLine]) -> str:
    rows: list[str] = []
    for line in lines:
        marker = f"<span style='color:{WARN}'>▲</span>" if line.warn else (
            f"<span style='color:{DIM}'>•</span>"
        )
        if line.kind == "item":
            colour = WARN if line.warn else TEXT
            rows.append(
                f"<tr><td width='18'>{marker}</td>"
                f"<td style='color:{colour};white-space:nowrap' width='150'><b>"
                f"{_escaped(line.label + ':')}</b></td>"
                f"<td style='color:{TEXT_SOFT}'>{_escaped(line.body)}</td></tr>"
            )
        elif line.kind == "heading":
            rows.append(
                f"<tr><td width='18'>{marker}</td><td colspan='2' style='color:{TEXT}'><b>"
                f"{_escaped(line.label)}</b>{_escaped(':')}</td></tr>"
            )
        elif line.kind == "note":
            rows.append(
                f"<tr><td></td><td></td><td style='color:{MUTED}'>{_note_html(line.body)}</td></tr>"
            )
        else:
            rows.append(
                f"<tr><td width='18'>{marker}</td><td colspan='2' style='color:{TEXT_SOFT}'>"
                f"{_escaped(line.body)}</td></tr>"
            )
    return f"<table cellspacing='0' cellpadding='4'>{''.join(rows)}</table>"


class _Signals(QObject):
    """Owned by the dialog, never by the task: a finished runnable dies on a pool thread."""

    done = pyqtSignal(str)


class _CheckTask(QRunnable):
    def __init__(self, config_data: dict, signals: _Signals) -> None:
        super().__init__()
        self._config_data = config_data
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            from ..app import _doctor_text
            from ..core.config import Config

            text = _doctor_text(Config(data=self._config_data))
        except Exception as exc:  # noqa: BLE001 - a partial answer beats a blank window
            log.debug("system check failed", exc_info=True)
            text = f"The check could not run: {exc}"
        with contextlib.suppress(RuntimeError):  # the window may be gone by now
            self._signals.done.emit(text or "The check printed nothing.")


class SystemCheckDialog(QDialog):
    def __init__(self, parent, config_data: dict) -> None:
        super().__init__(parent)
        self._config_data = config_data
        self._signals = _Signals(self)
        self._signals.done.connect(self._show_result)
        self.setWindowTitle("System check")
        self.resize(760, 540)
        layout = QVBoxLayout(self)
        self.intro = QLabel(
            "Looks at your graphics, Vulkan, the Wine runners and a few system settings. "
            "Nothing is changed and nothing is uploaded."
        )
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)
        self._raw = ""
        self.text = QTextBrowser()
        self.text.setObjectName("Report")  # the dark panel of the log viewer, set out as a list
        self.text.setOpenLinks(False)
        layout.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy to clipboard")
        self.copy_button.clicked.connect(self._copy)
        buttons.addWidget(self.copy_button)
        self.again_button = QPushButton("Check again")
        self.again_button.clicked.connect(self.run_check)
        buttons.addWidget(self.again_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def run_check(self) -> None:
        """Start the check (again) in the background."""
        self._raw = ""
        self.text.setPlainText("Checking your system…")
        self.again_button.setEnabled(False)
        QThreadPool.globalInstance().start(_CheckTask(self._config_data, self._signals))

    def _show_result(self, text: str) -> None:
        self._raw = text
        self.text.setHtml(report_html(text))
        self.again_button.setEnabled(True)

    def _copy(self) -> None:
        # The original text, so a bug report gets exactly what the command line prints.
        QApplication.clipboard().setText(self._raw or self.text.toPlainText())
