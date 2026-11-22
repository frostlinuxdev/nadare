# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Questions that ignore clicks for a moment after they open.

A button that opens a question can take a double-click: the first click opens the question and
the second one lands on whatever button of it is under the pointer. In the 2026-11 user
simulation that was a few pixels from "Stop and remove the game…". Holding the buttons for a
double-click's length keeps the second click from answering a question nobody has read yet.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

#: Longer than the second click of a double-click, short enough not to feel slow.
HOLD_MS = 450


def hold_ms() -> int:
    """The desktop's double-click interval, kept between 300 and 800 ms."""
    try:
        interval = int(QApplication.doubleClickInterval())
    except (RuntimeError, TypeError, ValueError):
        interval = HOLD_MS
    return max(300, min(800, interval or HOLD_MS))


def hold_clicks(box: QMessageBox, ms: int | None = None) -> None:
    """Disable ``box``'s buttons now and enable them ``ms`` later (default :func:`hold_ms`)."""
    buttons = list(box.buttons())
    for button in buttons:
        button.setEnabled(False)

    def release() -> None:
        for button in buttons:
            button.setEnabled(True)

    # A child of the question: answered and deleted early, the timer goes with it.
    timer = QTimer(box)
    timer.setSingleShot(True)
    timer.timeout.connect(release)
    timer.start(hold_ms() if ms is None else ms)
