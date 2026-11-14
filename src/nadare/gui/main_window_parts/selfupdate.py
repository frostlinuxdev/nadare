# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The update banner, the in-app self-update and the restart that follows.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    Qt,
    QUrl,
)
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QMessageBox,
    QProgressDialog,
)

from ... import __version__
from ...core import (
    diskspace,
    selfupdate,
)

log = logging.getLogger("nadare.gui.main_window")


def capitalised(text: str) -> str:
    """``text`` with its first letter in upper case (reasons are written to follow a sentence)."""
    return text[:1].upper() + text[1:]


class SelfUpdateMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    # ---- shutdown -----------------------------------------------------------
    # ---- update check ---------------------------------------------------------
    def _start_update_check(self) -> None:
        """Ask the release page once per start, off the GUI thread."""
        if not self.config.data.get("check_updates", True):
            return
        from ...core import channel, updatecheck

        if channel.is_unstable():
            # An unstable build is updated by hand: the stable release is not its next version.
            return

        if updatecheck.managed_updates():
            # The AUR/Flatpak package manager owns updates; a release-page
            # button would tempt the user into a second, mixed install.
            return

        def run() -> None:
            try:
                info = updatecheck.check_latest()
                if info is None:
                    return
                self.update_found.emit(info)
            except RuntimeError:
                pass  # the window closed before the check finished
            except Exception:  # noqa: BLE001 - a background check must never die
                log.debug("update check failed", exc_info=True)

        threading.Thread(target=run, name="nadare-update-check", daemon=True).start()

    def _show_update(self, info: Any) -> None:
        self._update_url = str(info.url)
        self._update_info = info
        self.update_button.setText(f"Update available: v{info.version}")
        self.update_button.setToolTip(f"{info.name}\n{info.url}")
        self.update_button.setVisible(True)
        self._update_version = str(info.version)
        if self.config.data.get("dismissed_update") != self._update_version:
            updatable = self._self_update.can_update(info)
            short, detail = ("", "") if updatable else self._download_only_reason(info)
            message = f"Nadare v{info.version} is available (you have v{__version__})."
            self.update_banner.show_message(
                f"{message} {short}" if short else message,
                "Update" if updatable else "Download",
            )
            self._set_update_tip(detail)

    def _dismiss_update(self) -> None:
        """Hide the banner for this release; the sidebar button stays."""
        self.update_banner.hide_banner()
        self.config.data["dismissed_update"] = self._update_version
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the dismissed update", exc_info=True)

    def _open_update_page(self) -> None:
        if self._update_url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(self._update_url))

    # ---- in-place update (#771) -----------------------------------------------
    def _on_update_clicked(self) -> None:
        """Update in place when this AppImage can replace itself, else open the release page."""
        if self._restart_target is not None:
            self._offer_restart()
        elif self._self_update.running and self._update_info is not None:
            if self._update_progress is not None:
                self._update_progress.show()
        elif self._update_info is not None and self._self_update.can_update(self._update_info):
            self._begin_self_update()
        else:
            self._open_update_page()

    def _begin_self_update(self) -> None:
        info = self._update_info
        dialog = QProgressDialog(
            f"Downloading Nadare v{info.version}…", "Cancel", 0, 0, self
        )
        dialog.setWindowTitle("Updating Nadare")
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self._self_update.cancel)
        self._update_progress = dialog
        if self._self_update.start(info):
            dialog.show()
        else:
            self._close_update_progress()

    def _close_update_progress(self) -> None:
        dialog, self._update_progress = self._update_progress, None
        if dialog is not None:
            with contextlib.suppress(Exception):
                dialog.canceled.disconnect()
            dialog.close()
            dialog.deleteLater()

    def _on_update_progress(self, done: int, total: int) -> None:
        dialog = self._update_progress
        if dialog is None:
            return
        if total > 0:
            dialog.setMaximum(total)
            dialog.setValue(min(done, total))
            dialog.setLabelText(
                f"Downloading Nadare v{self._update_version}… "
                f"{diskspace.human_size(done)} of {diskspace.human_size(total)}"
            )

    def _on_update_installed(self, new: object) -> None:
        self._close_update_progress()
        self._restart_target = Path(str(new))
        self.update_button.setText("Restart to finish updating")
        self.update_banner.show_message(
            f"Nadare v{self._update_version} is installed. Restart to start using it.",
            "Restart",
        )
        self._offer_restart()

    def _on_update_failed(self, message: str) -> None:
        self._close_update_progress()
        # Not again this run: the release page is the way to get this version.
        self._update_info = None
        self.update_banner.show_message(
            f"Nadare v{self._update_version} is available (you have v{__version__}). "
            "Nadare could not update itself; download it from the release page.",
            "Download",
        )
        self._set_update_tip("")
        self._show_update_failure(message)

    def _on_update_cancelled(self) -> None:
        self._close_update_progress()

    def _show_update_failure(self, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Update not installed")
        box.setText("Nadare could not update itself. Nothing was changed.")
        box.setInformativeText(
            f"{capitalised(message)}\n\nYou can download the new version by hand."
        )
        page = box.addButton("Open the download page", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is page:
            self._open_update_page()

    def _ask_restart(self, version: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Update installed")
        box.setText(f"Nadare v{version} is installed.")
        box.setInformativeText("Restart now to start using it, or restart later yourself.")
        restart = box.addButton("Restart now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is restart

    def _offer_restart(self) -> None:
        if self._restart_target is not None and self._ask_restart(self._update_version):
            self._restart_now()

    def _inform_busy_restart(self) -> None:
        QMessageBox.information(
            self,
            "Restart later",
            "A game or an install is still running. Finish or stop it first, then restart "
            "Nadare to start the new version.",
        )

    def _restart_now(self) -> None:
        target = self._restart_target
        if target is None:
            return
        removing = any(operation == "remove" for operation in self._busy.values())
        if self.controller.mutating_profiles() or self.controller.launching_profiles() or removing:
            self._inform_busy_restart()
            return
        # Close first and only go on if the window really closed: a prompt the user cancels
        # (or refuses) must stop the restart, not be overridden by it.
        if not self._close_window_for_update():
            return
        selfupdate.restart(target)
        self._quit_app()

    def _close_window_for_update(self) -> bool:
        self.close()
        return not self.isVisible()

    def _quit_app(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()
