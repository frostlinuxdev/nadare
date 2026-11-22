# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Safety check off the GUI thread, and the small window that shows its answer (#806).

The engine is :mod:`nadare.core.safetycheck`; this module only runs it and shows the result.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import UTC, datetime

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..core import safetycheck
from . import i18n
from .theme import MUTED, TEXT, WARN

log = logging.getLogger(__name__)


class SafetyCheckSignals(QObject):
    """Owned by the window, never by the task: a finished runnable dies on a pool thread."""

    finished = pyqtSignal(str, object, object)  # profile name, SafetyResult, generation


class SafetyCheckTask(QRunnable):
    """Load the signed list and check one profile off the GUI thread."""

    def __init__(self, profile: str, profile_data: dict, signals: SafetyCheckSignals) -> None:
        super().__init__()
        self.generation = object()
        self.profile = profile
        self._profile_data = dict(profile_data)
        self._signals = signals
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            indicators = safetycheck.load_indicators()
            result = safetycheck.check_profile(
                self.profile, self._profile_data, indicators, cancel=self._cancel
            )
        except Exception:  # noqa: BLE001 - a blank button is worse than a plain failure
            log.debug("safety check failed", exc_info=True)
            result = safetycheck.SafetyResult(level=safetycheck.ERROR)
        with contextlib.suppress(RuntimeError):  # the window may be gone by now
            self._signals.finished.emit(self.profile, result, self.generation)


_HEADINGS = {
    safetycheck.CLEAN: ("Clean", TEXT),
    safetycheck.INCOMPLETE: ("Incomplete", WARN),
    safetycheck.EXPOSED: ("Exposed", WARN),
    safetycheck.COMPROMISED: ("Compromised", WARN),
    # No list: the summary is the whole answer, so a heading would repeat it (#b28).
    safetycheck.NO_LIST: ("", MUTED),
    safetycheck.CANCELLED: ("Check cancelled", MUTED),
    safetycheck.ERROR: ("The check could not run", WARN),
}

#: Design width of the result window; the height follows the text (#b28).
_CONTENT_WIDTH = 460


def _local_time(stamp: str) -> str:
    """``checked_at`` in the reader's own time zone, never the raw ISO text (#b28)."""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:  # a saved or hand-edited stamp without a zone
        moment = moment.replace(tzinfo=UTC)
    return i18n.format_datetime(moment.astimezone())


def _when_text(result: safetycheck.SafetyResult, title: str) -> str:
    """When it ran (or was tried) in local time, and for which game by title (#b28)."""
    when = _local_time(result.checked_at)
    verb = i18n.tr("Tried") if result.level == safetycheck.NO_LIST else i18n.tr("Checked")
    line = f"{verb} {when}" if when else verb
    return " · ".join(part for part in (line, title) if part)


class SafetyResultDialog(QDialog):
    """The full answer: level, what matched (ids and counts only), and when it ran."""

    def __init__(self, parent, result: safetycheck.SafetyResult, title: str = "") -> None:
        super().__init__(parent)
        self.result = result
        self.setWindowTitle(i18n.tr("Safety check"))
        layout = QVBoxLayout(self)
        heading, colour = _HEADINGS.get(result.level, (result.level, TEXT))
        self.heading = QLabel(heading)
        self.heading.setStyleSheet(f"color: {colour}; font-size: 18px; font-weight: 700;")
        self.heading.setVisible(bool(heading))
        layout.addWidget(self.heading)
        self.summary = QLabel(safetycheck.result_summary(result))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        details = []
        for match in result.matches:
            noun = "file" if match.count == 1 else "files"
            details.append(f"{match.id}: {match.count} {noun}")
        self.details = QLabel("\n".join(details))
        self.details.setWordWrap(True)
        self.details.setVisible(bool(details))
        layout.addWidget(self.details)
        self.when = QLabel(_when_text(result, title))
        self.when.setObjectName("Muted")
        self.when.setStyleSheet(f"color: {MUTED};")
        layout.addWidget(self.when)
        layout.addStretch(1)  # content stays at the top if the user enlarges the window
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(i18n.tr("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._fit_to_contents()

    def _fit_to_contents(self) -> None:
        """Size the window to its answer instead of a fixed box with empty space (#b28)."""
        layout = self.layout()
        layout.activate()
        height = layout.heightForWidth(_CONTENT_WIDTH)
        self.resize(_CONTENT_WIDTH, height if height > 0 else self.sizeHint().height())


__all__ = ["SafetyCheckSignals", "SafetyCheckTask", "SafetyResultDialog"]
