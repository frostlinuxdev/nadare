# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Nadare's PyQt6 umbrella GUI.

Public entry points::

    from nadare.gui import open_window
    window = open_window(config)          # creates a QApplication if needed

The GUI talks to games only through ``core.backends.create`` and the
``GameBackend`` contract, always on a worker thread (see ``workers.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import Config

__all__ = ["open_window", "run"]

log = logging.getLogger(__name__)

#: ``run()`` exits 0 when a second launch activated the running window. A lock
#: that could not be created means no window opened and gets its own non-zero
#: status (finding 06-10).
EXIT_ACTIVATED_OTHER = 0
EXIT_NO_SINGLE_INSTANCE = 1


def _application():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def open_window(config: Config | None = None, profile: str | None = None, app: Any | None = None):
    """Create, show and return the Nadare main window.

    ``config`` defaults to :meth:`Config.load`; ``profile`` selects an initial
    profile (the ``--profile`` flag). Reuses an existing ``QApplication`` unless
    one is passed explicitly.
    """
    from ..core import paths

    paths.ensure_dirs()
    application = app if app is not None else _application()
    from .main_window import MainWindow
    from .theme import apply_theme

    apply_theme(application)
    cfg = config if config is not None else Config.load()
    window = MainWindow(cfg, profile=profile)
    window.show()
    return window


def _activate(window: Any) -> None:
    """Raise, unminimise and focus the existing window."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    window.setWindowState(
        (window.windowState() & ~Qt.WindowState.WindowMinimized)
        | Qt.WindowState.WindowActive
    )
    window.show()
    window.raise_()
    window.activateWindow()
    QApplication.alert(window)


def run(config: Config | None = None, profile: str | None = None) -> int:
    """Open the window and run the Qt event loop. Returns the exit code.

    The GUI is single-instance per user: a second start asks the running window
    to activate and exits ``EXIT_ACTIVATED_OTHER`` (0) without opening anything;
    a lock that could not be created exits ``EXIT_NO_SINGLE_INSTANCE``.
    """
    from ..core import tls

    tls.ensure_ca_bundle()
    application = _application()
    cfg = config if config is not None else Config.load()

    instance = None
    if cfg.data.get("lock_to_single_instance", True):
        from .single_instance import SingleInstance

        instance = SingleInstance()
        if not instance.start():
            return _single_instance_exit(instance)

    window = open_window(cfg, profile=profile, app=application)
    if cfg.notice:
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.information(window, "Nadare", cfg.notice)
    if instance is not None:
        instance.activation_requested.connect(lambda: _activate(window))
        window._single_instance = instance  # keep the server alive with the window
    code = application.exec()
    controller = getattr(window, "controller", None)
    window.deleteLater()
    from .workers import wait_detached

    if not wait_detached(5000):
        # A fetch is still blocked in the network; letting Python tear down its
        # running QThread would abort with a core dump, so leave immediately.
        # Give an install/update that is still writing a longer grace period
        # first, so the hard exit cannot truncate the game files (06-5).
        _wait_for_mutating_workers(controller)
        import os

        logging.shutdown()
        os._exit(int(code))
    return int(code)


def _single_instance_exit(instance: Any) -> int:
    """The exit code after ``SingleInstance.start()`` returned False.

    ``instance.status`` tells the two cases apart: a live server answered and
    was asked to activate (a second launch is not an error) versus a lock that
    could not be created, which must not exit as a silent success (06-10).
    """
    status = str(getattr(instance, "status", "") or "")
    if "already running" in status:
        log.info("another Nadare instance is already running; asked it to activate")
        return EXIT_ACTIVATED_OTHER
    log.error("the GUI cannot start: %s", status or "single-instance lock unavailable")
    return EXIT_NO_SINGLE_INSTANCE


def _wait_for_mutating_workers(controller: Any, wait_ms: int = 15000) -> bool:
    """Wait out an in-flight install/update before a hard ``os._exit``.

    Returns True when a mutating worker was found and the controller was asked
    to stop it (with a longer deadline than the initial shutdown), False when
    only non-mutating fetches remain. Never raises: it runs on the teardown
    path (06-5).
    """
    if controller is None:
        return False
    try:
        if not controller.mutating_profiles():
            return False
        controller.shutdown(wait_ms=wait_ms)
        return True
    except Exception:  # noqa: BLE001 - teardown must never raise
        log.debug("waiting for mutating workers failed", exc_info=True)
        return False
