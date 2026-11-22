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

from .. import i18n, steam_setup

log = logging.getLogger("nadare.gui.main_window")

#: Downloads that closing Nadare stopped, ``[[profile, operation], ...]``, offered back once.
INTERRUPTED_KEY = "interrupted_downloads"
#: What may be started again without asking anything new (an install payload is always empty).
RESTARTABLE_DOWNLOADS = frozenset({"install", "update", "repair"})


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
        names = ", ".join(sorted(self._game_title(n) for n in active))
        # Only an install/update/removal leaves half-written game files; a login
        # just gets cancelled.
        writes_files = any(
            self.controller.running_operation(n)
            in {"install", "update", "repair", "uninstall", "remake_prefix"}
            for n in active
        )
        if writes_files:
            title = "Operation in progress"
            waiting = [self._game_title(n) for n in self.controller.queue_order()]
            line = (
                f"\n\nWaiting in line: {', '.join(waiting)}. Nadare offers to continue "
                "these downloads the next time it starts."
                if waiting
                else ""
            )
            text = (
                f"{names} is still installing, updating or rebuilding its Wine prefix.\n\n"
                "Closing stops it. What is already downloaded is kept and RESUME carries on "
                "from there. Nadare offers to continue downloads next time it starts. "
                f"The game files stay incomplete until you do.{line}\n\n"
                "Close anyway?"
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

    # ---- downloads stopped by closing Nadare (user sim P1 #3) ------------------
    def _remember_interrupted_downloads(self) -> None:
        """Write down the installs closing stops, running first, then the line in its order."""
        playing = set(self.controller.launching_profiles())
        stopped: list[list[str]] = []
        for name in self.controller.mutating_profiles():
            operation = self.controller.running_operation(name)
            if name not in playing and operation in RESTARTABLE_DOWNLOADS:
                stopped.append([name, operation])
        for name in self.controller.queue_order():
            operation = self.controller.waiting_operation(name)
            if operation in RESTARTABLE_DOWNLOADS and [name, operation] not in stopped:
                stopped.append([name, operation])
        try:
            with self.config.locked():
                if stopped:
                    self.config.data[INTERRUPTED_KEY] = stopped
                elif self.config.data.pop(INTERRUPTED_KEY, None) is None:
                    return
                self.config.save()
        except OSError:
            log.warning("could not remember the downloads closing stopped", exc_info=True)

    def _offer_interrupted_when_shown(self) -> None:
        """The start-up timer's entry: a window started hidden keeps the record for later."""
        if self.isVisible() and not self._closing:
            self._offer_interrupted_downloads()

    def _offer_interrupted_downloads(self) -> None:
        """Ask once whether to continue the downloads the last close stopped."""
        record = self.config.data.get(INTERRUPTED_KEY)
        if record is None:
            return
        entries: list[tuple[str, str]] = []
        if isinstance(record, list):
            for entry in record:
                if (
                    isinstance(entry, list)
                    and len(entry) == 2
                    and entry[0] in self.config.profiles
                    and entry[1] in RESTARTABLE_DOWNLOADS
                    and (entry[0], entry[1]) not in entries
                ):
                    entries.append((entry[0], entry[1]))
        try:
            with self.config.locked():
                self.config.data.pop(INTERRUPTED_KEY, None)
                self.config.save()
        except OSError:
            log.warning("could not forget the stopped downloads", exc_info=True)
        if not entries:
            return
        if not self._ask_continue_downloads([self._game_title(name) for name, _ in entries]):
            return
        for name, operation in entries:
            # The first one starts, the rest queue behind it like they did before the close.
            self._start(name, operation, silent=True)

    def _ask_continue_downloads(self, titles: list[str]) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(i18n.tr("Continue downloads"))
        box.setText(i18n.tr("Continue the downloads Nadare stopped when it closed?"))
        box.setInformativeText(
            i18n.tr(
                "{games}. What was already downloaded is kept, so they carry on from where "
                "they stopped.",
                games=", ".join(titles),
            )
        )
        carry_on = box.addButton(i18n.tr("Continue downloads"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(i18n.tr("Not now"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(carry_on)
        try:
            box.exec()
            return box.clickedButton() is carry_on
        finally:
            box.deleteLater()
