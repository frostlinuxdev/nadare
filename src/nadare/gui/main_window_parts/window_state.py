# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Window geometry and the close confirmation.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import (
    QByteArray,
    QRect,
    QSettings,
    QSize,
    QTimer,
)
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QMessageBox,
)

from .. import steam_setup

log = logging.getLogger("nadare.gui.main_window")


def _window_minimum(minimum: QSize, area: QRect) -> QSize:
    """The window minimum, lowered when the screen is smaller than it.

    A minimum larger than the desktop is what pushes footer buttons off a
    scaled laptop screen; the layout can still scroll, so the screen wins.
    """
    return QSize(
        min(minimum.width(), area.width()),
        min(minimum.height(), area.height()),
    )


def _window_size(minimum: QSize, area: QRect) -> QSize:
    """First-run window size: half the screen wide, almost all of it high, never over it."""
    width = max(minimum.width(), area.width() // 2)
    # leave room for the title bar and window border, which resize() excludes
    height = max(minimum.height(), area.height() - 48)
    return QSize(min(width, area.width()), min(height, area.height()))


class WindowStateMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    # ---- window geometry ------------------------------------------------------
    def _geometry_settings(self) -> QSettings:
        """Window size/position, kept beside the config (not inside it)."""
        path = self.config.path.with_name("window.ini")
        return QSettings(str(path), QSettings.Format.IniFormat)

    def _restore_geometry(self) -> None:
        """Reopen where the user left it; first run takes half the screen."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1180, 780)
            return
        area = screen.availableGeometry()
        # A 150%-scaled laptop can report a logical screen shorter than the
        # designed minimum; never keep a minimum the desktop cannot show.
        self.setMinimumSize(_window_minimum(self.minimumSize(), area))
        saved = self._geometry_settings().value("geometry")
        if isinstance(saved, QByteArray) and self.restoreGeometry(saved):
            # The saved size can belong to a larger monitor.
            size = self.size()
            if size.width() > area.width() or size.height() > area.height():
                self.resize(
                    min(size.width(), area.width()), min(size.height(), area.height())
                )
            return
        self.resize(_window_size(self.minimumSize(), area))
        self.move(area.topLeft())

    def _save_geometry(self) -> None:
        try:
            settings = self._geometry_settings()
            settings.setValue("geometry", self.saveGeometry())
            settings.sync()
        except Exception:  # noqa: BLE001 - never block closing on this
            pass

    def _request_close_from_signal(self) -> None:
        """Close like the window button, but never wait on a question (SIGTERM/SIGINT)."""
        self._close_requested_by_signal = True
        self.close()

    def _finish_background_close(self) -> None:
        """The last game exited while the window was hidden: really quit now."""
        if self.isVisible():
            return
        self.controller.shutdown()
        self.predownloads.shutdown()
        QApplication.quit()

    def _maybe_finish_background_close(self) -> None:
        """Quit after a hidden close once no launch or mutation is still running."""
        if (
            self._quit_after_games
            and not self.controller.launching_profiles()
            and not self.controller.mutating_profiles()
        ):
            if steam_setup.steps_running():  # a Set up step must not be cut off: look again soon
                self._finish_timer.start(1000)
                return
            self._quit_after_games = False
            QTimer.singleShot(0, self._finish_background_close)

    def _confirm_close(self) -> bool:
        """Warn before quitting while an install/update is still writing.

        The worker is cancelled cooperatively on shutdown, but if it misses the
        cancel window the process may be torn down mid-write; the user should
        choose that knowingly. A running game never needs a warning: closing
        the window keeps it running.
        """
        if steam_setup.steps_running():
            return self._ask_steam_step_close()
        playing = set(self.controller.launching_profiles())
        removing = sorted(n for n, op in self._busy.items() if op == "remove")
        if removing:
            # Remove runs on the global thread pool, not the controller, and the
            # profile is only dropped from config when it ends.
            answer = QMessageBox.warning(
                self,
                "Removal in progress",
                f"{', '.join(removing)} is still being removed.\n\n"
                "Closing now can leave the game half removed and still listed. "
                "Close anyway?",
                QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return answer == QMessageBox.StandardButton.Close
        active = [n for n in self.controller.mutating_profiles() if n not in playing]
        if not active:
            return True
        names = ", ".join(sorted(active))
        # Only an install/update/removal leaves half-written game files; a login
        # just gets cancelled.
        writes_files = any(
            self.controller.running_operation(n)
            in {"install", "update", "repair", "uninstall", "remake_prefix"}
            for n in active
        )
        if writes_files:
            title = "Operation in progress"
            text = (
                f"{names} is still installing, updating or rebuilding its Wine prefix.\n\n"
                "Closing stops it. What is already downloaded is kept and INSTALL carries on "
                "from there, but the game files stay incomplete until you do. Close anyway?"
            )
        else:
            title = "Login in progress"
            text = f"{names} is still signing in or changing its login.\n\nClose anyway?"
        answer = QMessageBox.warning(
            self,
            title,
            text,
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Close
