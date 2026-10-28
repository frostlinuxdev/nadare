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
import signal
import sys
from typing import Any

from ..core import sandbox
from ..core.config import Config, ConfigReadError
from ..core.shortcuts import FLATPAK_ID

__all__ = ["open_window", "run"]

log = logging.getLogger(__name__)

#: ``run()`` exits 0 when a second launch activated the running window. A lock
#: that could not be created means no window opened and gets its own non-zero
#: status (finding 06-10).
EXIT_ACTIVATED_OTHER = 0
EXIT_NO_SINGLE_INSTANCE = 1


def _cli_invocation() -> str:
    """How this build is started from a terminal (AppImage, Flatpak or ``nadare``)."""
    import os
    import shlex

    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return shlex.quote(appimage)
    if sandbox.in_flatpak():
        return f"flatpak run {FLATPAK_ID}"
    program = sys.argv[0] if sys.argv and sys.argv[0] else "nadare"
    name = os.path.basename(program)
    if name == "python" or name.startswith("python3"):
        return "nadare"
    return shlex.quote(program)


def _require_display() -> None:
    """Exit cleanly, with a hint, when there is no display to open a window on.

    Qt aborts the whole process (SIGABRT) when it finds no platform plugin, long
    before any Python error handling could say what is wrong. ``QT_QPA_PLATFORM``
    (offscreen, xcb, ...) set by the user skips the check.
    """
    import os

    if os.environ.get("QT_QPA_PLATFORM") or os.environ.get("DISPLAY"):
        return
    if os.environ.get("WAYLAND_DISPLAY"):
        return
    command = _cli_invocation()
    raise SystemExit(
        "nadare: no display found (DISPLAY and WAYLAND_DISPLAY are unset), so the window "
        "cannot open. Run it from a desktop session, or use the command line: "
        f"{command} list | install | launch -p <game>."
    )


class EnterActivatesButtons:
    """Make Enter press the focused push button, not just Space.

    Qt only does this inside dialogs; on the main window a keyboard user who
    Tabs to a button and presses Enter would see nothing happen.
    """

    @staticmethod
    def install(application: Any) -> Any:
        from PyQt6.QtCore import QEvent, QObject, Qt
        from PyQt6.QtWidgets import QDialog, QPushButton

        class _Filter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
                if (
                    event.type() == QEvent.Type.KeyPress
                    and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and isinstance(obj, QPushButton)
                    and obj.isEnabled()
                    and obj.hasFocus()
                    and not isinstance(obj.window(), QDialog)
                ):
                    obj.click()
                    return True
                return False

        keyfilter = _Filter(application)
        application.installEventFilter(keyfilter)
        return keyfilter


def _application():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        from . import nativedialogs

        nativedialogs.apply()
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
    EnterActivatesButtons.install(application)
    cfg = config if config is not None else Config.load()
    window = MainWindow(cfg, profile=profile)
    window.show()
    return window


def _install_signal_handlers(window: Any) -> dict[int, Any]:
    """Close the window when the desktop sends SIGTERM/SIGINT.

    Going through ``close`` (rather than the default handler) runs the same
    controller shutdown as the window button, so an installer child is stopped
    instead of being left in its own session.
    """
    from PyQt6.QtCore import QTimer

    previous: dict[int, Any] = {}

    def _request_close(signum: int, _frame: Any) -> None:
        log.info("received signal %s; closing Nadare", signum)
        try:
            QTimer.singleShot(0, window._request_close_from_signal)
        except RuntimeError:
            pass  # the window was already destroyed

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, _request_close)
        except (ValueError, OSError):
            pass  # not on the main thread; leave the default behaviour
    if previous:
        # Qt's loop does not return to Python on its own, so a pending signal
        # would wait for the next GUI event; a cheap tick lets the handler run.
        tick = QTimer(window)
        tick.timeout.connect(lambda: None)
        tick.start(250)
        window._signal_tick = tick
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, TypeError):
            pass


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


def _config_recovery_text(cfg: Config) -> str:
    """Add a plain next step to a quarantined-config notice (#332 F-08)."""
    text = cfg.notice or ""
    if not text or not any(
        marker in text
        for marker in ("unreadable", "were reset", "no profiles section")
    ):
        return text
    import re

    match = re.search(r"(?:moved to|saved to) (.+?)(?: and a fresh config|$)", text)
    backup = match.group(1).strip() if match else str(cfg.path)
    return (
        f"{text}\n\nYour settings were reset. A copy of the old file was kept at "
        f"{backup}. You can add your games again."
    )


def run(config: Config | None = None, profile: str | None = None) -> int:
    """Open the window and run the Qt event loop. Returns the exit code.

    The GUI is single-instance per user: a second start asks the running window
    to activate and exits ``EXIT_ACTIVATED_OTHER`` (0) without opening anything;
    a lock that could not be created exits ``EXIT_NO_SINGLE_INSTANCE``.
    """
    from ..app import warn_if_root
    from ..core import tls

    warn_if_root()
    _require_display()
    tls.setup()
    application = _application()
    try:
        cfg = config if config is not None else Config.load()
    except (ConfigReadError, OSError) as exc:
        return _fatal(
            "Nadare cannot read its settings",
            f"{exc}\n\nCheck that this file or folder is readable by your user, then start "
            "Nadare again.",
        )

    instance = None
    if cfg.data.get("lock_to_single_instance", True):
        from .single_instance import SingleInstance

        instance = SingleInstance()
        if not instance.start():
            code = _single_instance_exit(instance)
            if code == EXIT_NO_SINGLE_INSTANCE:
                status = str(getattr(instance, "status", "") or "")
                _fatal(
                    "Nadare cannot start",
                    "Nadare could not set up its single-window lock"
                    + (f" ({status})" if status else "")
                    + ".\n\nIf you started it from a desktop icon, check that your runtime "
                    "folder ($XDG_RUNTIME_DIR) exists and is writable, then try again.",
                )
            return code

    try:
        window = open_window(cfg, profile=profile, app=application)
    except OSError as exc:
        return _fatal(
            "Nadare cannot start",
            f"{exc}\n\nNadare could not create its data folders. Check that your home folder "
            "is writable and has free space.",
        )
    previous_handlers = _install_signal_handlers(window)
    if cfg.notice:
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.information(window, "Nadare", _config_recovery_text(cfg))
    if instance is not None:
        instance.activation_requested.connect(lambda: _activate(window))
        window._single_instance = instance  # keep the server alive with the window
    try:
        code = application.exec()
    finally:
        _restore_signal_handlers(previous_handlers)
    controller = getattr(window, "controller", None)
    window.deleteLater()
    from .workers import wait_detached

    if controller is not None:
        # A queued success callback may have raced the last closeEvent; make
        # sure no fresh status worker survives it.
        try:
            controller.shutdown()
        except Exception:  # noqa: BLE001 - teardown must never raise
            log.debug("final controller shutdown failed", exc_info=True)
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
    problem = status or "the single-instance lock is unavailable"
    log.error("the GUI cannot start: %s", problem)
    print(
        f"[error] Nadare cannot set up its single-window lock: {problem}. "
        "Set lock_to_single_instance to false in your settings to start without it.",
        file=sys.stderr,
    )
    return EXIT_NO_SINGLE_INSTANCE


def _fatal(title: str, text: str) -> int:
    """Tell the user why Nadare cannot start (a desktop launch has no terminal) and exit 1."""
    log.error("%s: %s", title, text.replace("\n", " "))
    print(f"[error] {title}: {text}", file=sys.stderr)
    try:
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, title, text)
    except Exception:  # noqa: BLE001 - nothing more can be done without a display
        pass
    return 1


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
