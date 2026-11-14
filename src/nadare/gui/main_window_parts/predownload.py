# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Predownload: start, cancel and follow the next version's download (#1273).

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QMessageBox,
)

log = logging.getLogger("nadare.gui.main_window")


class PredownloadMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _start_predownload(self) -> None:
        """Start the predownload on its own controller, never on the one Play uses."""
        profile = self._current_profile
        if not profile:
            return
        if not self.predownloads.start(profile, "predownload"):
            self._show_predownload_error(
                profile, "A predownload for this game is already running."
            )

    def _cancel_predownload(self) -> None:
        if self._current_profile:
            self.predownloads.cancel(self._current_profile)

    def _on_predownload_busy(self, profile: str, operation: str, active: bool) -> None:
        if active:
            self._predownload_running.add(profile)
        else:
            self._predownload_running.discard(profile)
        if profile == self._current_profile:
            self.game.set_predownload_progress(active)

    def _on_predownload_event(self, profile: str, event: object) -> None:
        """Progress from a predownload updates only its button, not the normal banner."""
        if (
            profile == self._current_profile
            and getattr(event, "kind", "") == "progress"
            and getattr(event, "progress", None) is not None
        ):
            self.game.set_predownload_progress(True, event.progress)

    def _predownload_idle(self, profile: str) -> None:
        self._predownload_running.discard(profile)
        if profile == self._current_profile:
            self.game.set_predownload_progress(False)

    def _on_predownload_succeeded(self, profile: str, operation: str, code: int) -> None:
        self._predownload_idle(profile)
        # A fresh status is what turns the button into "x.y predownloaded".
        self.controller.start(profile, "status")

    def _on_predownload_failed(self, profile: str, operation: str, message: str) -> None:
        self._predownload_idle(profile)
        self._show_predownload_error(profile, message)

    def _on_predownload_cancelled(self, profile: str, operation: str) -> None:
        self._predownload_idle(profile)

    def _show_predownload_error(self, profile: str, message: str) -> None:
        """The one place a predownload error is shown (tests replace this)."""
        QMessageBox.warning(
            self, "Predownload", f"{self._game_title(profile)}: {message}"
        )
