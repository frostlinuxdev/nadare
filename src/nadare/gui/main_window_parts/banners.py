# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The graphics-driver banner and the notices banner.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PyQt6.QtCore import (
    QUrl,
)
from PyQt6.QtGui import QDesktopServices, QGuiApplication

from ... import __version__
from ...core import (
    flatpakgl,
)

log = logging.getLogger("nadare.gui.main_window")


def graphics_banner_text(result: Any) -> tuple[str, str] | None:
    """The startup banner for a Flatpak graphics problem, or None when all is well."""
    if result is None or result.ok:
        return None
    return result.summary, "Install" if result.refs else "Copy command"


class BannersMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _show_graphics_banner(self) -> None:
        """Warn at startup when the Flatpak lacks the host driver's graphics package."""
        found = flatpakgl.problem()
        shown = graphics_banner_text(found)
        if found is None or shown is None:
            return
        self._graphics_fix = found.fix
        self._graphics_refs = found.refs
        text, action = shown
        self.graphics_banner.show_message(text, action)

    def _on_graphics_action(self) -> None:
        if self._graphics_refs:
            self._install_graphics()
        else:
            self._copy_graphics_fix()

    def _install_graphics(self) -> None:
        """Install the missing packages off the GUI thread, then report back."""
        refs = self._graphics_refs
        if not refs or self._installing_graphics:
            return
        self._installing_graphics = True
        self.graphics_banner.action_button.setEnabled(False)
        self.graphics_banner.action_button.setText("Installing…")

        def run() -> None:
            try:
                ok, message = flatpakgl.install_missing(refs)
            except Exception as exc:  # noqa: BLE001 - never leave the banner "Installing…" (b203)
                ok, message = False, f"Installing the graphics packages failed ({exc})."
            try:
                self.graphics_installed.emit(ok, message)
            except RuntimeError:
                pass  # the window closed before the install finished

        threading.Thread(target=run, name="nadare-flatpak-install", daemon=True).start()

    def _graphics_install_finished(self, ok: bool, message: str) -> None:
        self._installing_graphics = False
        self._graphics_refs = ()
        self.graphics_banner.message_label.setText(message)
        self.graphics_banner.action_button.setEnabled(True)
        if ok:
            self.graphics_banner.action_button.setVisible(False)
        else:
            self.graphics_banner.action_button.setText("Copy command")
            self.graphics_banner.action_button.setVisible(True)

    def _copy_graphics_fix(self) -> None:
        QGuiApplication.clipboard().setText(self._graphics_fix)
        self.graphics_banner.action_button.setText("Copied")

    def _start_notices_check(self) -> None:
        """Read the project's notices feed once per start, off the GUI thread."""
        if not self.config.data.get("check_notices", True):
            return
        from ...core import notices

        def run() -> None:
            try:
                self.notices_found.emit(notices.load())
            except RuntimeError:
                pass  # the window closed before the feed arrived
            except Exception:  # noqa: BLE001 - a background check must never die
                log.debug("notices check failed", exc_info=True)

        threading.Thread(target=run, name="nadare-notices", daemon=True).start()

    def _show_notice(self, items: Any) -> None:
        from ...core import notices

        kinds = {str(p.get("backend") or "") for p in self.config.profiles.values()}
        dismissed = self.config.data.get("dismissed_notices") or []
        self._notice = notices.pick(items, __version__, kinds, dismissed=dismissed)
        if self._notice is None:
            self.notice_banner.hide_banner()
            return
        self.notice_banner.show_message(
            self._notice.text,
            "Learn more" if self._notice.link else "",
            dismissible=self._notice.severity != "block",
        )

    def _open_notice_link(self) -> None:
        link = self._notice.link if self._notice is not None else None
        if link and link.startswith("https://"):
            QDesktopServices.openUrl(QUrl(link))

    def _save_notice_preference(self, expanded: bool) -> None:
        """Remember whether "How this game runs" is open, for every game (#992)."""
        try:
            with self.config.locked():
                self.config.data["page_notice_expanded"] = bool(expanded)
                self.config.save()
        except OSError:
            log.debug("could not remember the notice choice", exc_info=True)

    def _save_console_preference(self, expanded: bool) -> None:
        """Remember Show/Hide output for every game (#993)."""
        try:
            with self.config.locked():
                self.config.data["output_expanded"] = bool(expanded)
                self.config.save()
        except OSError:
            log.debug("could not remember the output choice", exc_info=True)

    def _dismiss_notice(self) -> None:
        if self._notice is None:
            return
        self.notice_banner.hide_banner()
        seen = [str(x) for x in (self.config.data.get("dismissed_notices") or [])]
        self.config.data["dismissed_notices"] = [*seen, self._notice.id][-100:]
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the dismissed notice", exc_info=True)
