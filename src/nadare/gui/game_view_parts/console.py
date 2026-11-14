# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The output console, the error box and the failure choice.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QMessageBox,
)

from ...core import friendly, hostlibs, userns
from ..logs_dialog import LogsDialog
from ..userns_dialog import UsernsDialog

#: Log line colours by event kind.
_LOG_COLORS = {
    "error": "#ff8a8a",
    "done": "#6ea8ff",
    "status": "#85868d",
    "step": "#8ab4ff",
    "progress": "#85868d",
    "log": "#c7cad0",
}


class ConsoleMixin:
    """Mixin for :class:`GameView`; see the module text."""

    def _output_has_content(self) -> bool:
        return self._busy_op is not None or not self.console.document().isEmpty()

    def _sync_progress_frame(self) -> None:
        """Show the progress strip only while there is a step to report."""
        self.progress_frame.setVisible(
            self._busy_op is not None or bool(self.step_label.text().strip())
        )

    def _set_console_expanded(self, expanded: bool) -> None:
        self._console_expanded = bool(expanded)
        has_content = self._output_has_content()
        self.output_head.setVisible(has_content)
        self.console.setVisible(self._console_expanded and has_content)
        self.console_toggle.setText("Hide output" if self._console_expanded else "Show output")

    def _clear_console(self) -> None:
        self.console.clear()
        self._last_line = ""
        self._set_console_expanded(self._console_expanded)

    def _toggle_console(self) -> None:
        self._set_console_expanded(not self._console_expanded)
        self._console_preference = self._console_expanded
        self.console_preference_changed.emit(self._console_expanded)

    def set_console_preference(self, expanded: bool) -> None:
        """The saved Show/Hide output choice; an error still opens the output either way."""
        self._console_preference = bool(expanded)
        if self._busy_op is None:
            self._set_console_expanded(self._console_preference)

    def append_line(self, message: str, kind: str = "log") -> None:
        """Add one line to the page console (for results that arrive outside an operation)."""
        self._append_line(message, kind)

    def _append_line(self, message: str, kind: str = "log") -> None:
        if not message:
            return
        self._last_line = str(message)
        color = _LOG_COLORS.get(kind, _LOG_COLORS["log"])
        safe = html.escape(str(message)).replace("\n", "<br>")
        self.console.appendHtml(f'<span style="color:{color}">{safe}</span>')
        self._set_console_expanded(self._console_expanded)
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---- dialogs -----------------------------------------------------------
    def show_error(
        self, title: str, message: str, retry_op: str | None = None, *, failed: bool = False
    ) -> None:
        """Tell the user something went wrong.

        ``failed`` is for an operation that ended badly: the box then offers
        Open logs, and Retry when ``retry_op`` can be run again as it was.
        """
        self._set_console_expanded(True)
        if userns.has_hint(message):
            dialog = UsernsDialog(message, self, retry_op=retry_op)
            dialog.exec()
            if dialog.retry_requested and retry_op is not None:
                self.action_requested.emit(retry_op)
        elif not failed:
            QMessageBox.warning(self, title, message)
        else:
            # What the RETRY button re-runs if this box is closed.
            self._failed_op = retry_op
            choice = self._failure_choice(title, message, retry_op is not None)
            if choice == "retry" and retry_op is not None:
                self.action_requested.emit(retry_op)
            elif choice == "logs":
                self._open_logs()

    def _failure_choice(self, title: str, message: str, can_retry: bool) -> str:
        """Ask what to do about a failure: ``"retry"``, ``"logs"`` or ``"close"``."""
        plain, details = friendly.split(message)
        box = QMessageBox(QMessageBox.Icon.Warning, title, plain, parent=self)
        if details:
            box.setDetailedText(details)  # the raw text, behind "Show Details..."
        retry = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole) if can_retry else None
        logs = box.addButton("Open logs", QMessageBox.ButtonRole.ActionRole)
        # Missing 32-bit libraries come with a command: copy it instead of retyping (#338 F-08).
        command = hostlibs.install_command_in(message)
        copy = (
            box.addButton("Copy install command", QMessageBox.ButtonRole.ActionRole)
            if command
            else None
        )
        close = box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(retry or close)
        box.setEscapeButton(close)
        box.exec()
        clicked = box.clickedButton()
        if copy is not None and clicked is copy:
            QApplication.clipboard().setText(command)
            return "close"
        if retry is not None and clicked is retry:
            return "retry"
        return "logs" if clicked is logs else "close"

    def _open_logs(self) -> None:
        self._logs_dialog = LogsDialog([self._profile], profile=self._profile, parent=self)
        self._logs_dialog.report_requested.connect(self.report_requested.emit)
        self._logs_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._logs_dialog.show()
