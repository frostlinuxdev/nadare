# SPDX-License-Identifier: GPL-3.0-or-later
"""End the windows earlier tests left alive, before a test restyles the whole app (#1024)."""

from __future__ import annotations


def end_leftover_windows(wait_ms: int = 3000) -> int:
    """Delete every top-level widget now; returns how many there were.

    Background work is waited for first (bounded, as the autouse fixture in conftest.py does),
    because a window whose worker thread is still running must not be torn down under it.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent, QThreadPool
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return 0
    QThreadPool.globalInstance().waitForDone(wait_ms)
    try:
        from nadare.gui import workers

        workers.wait_detached(wait_ms)
    except Exception:  # noqa: BLE001 - best effort, like the conftest wait
        pass
    windows = list(QApplication.topLevelWidgets())
    for window in windows:
        window.hide()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    return len(windows)
