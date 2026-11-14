# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Offer a game found in another folder instead of downloading it again (#1293).

The check runs off the GUI thread on a copy of the config. Everything that changes the
profile happens on the GUI thread and only after the user picks a folder; no game file is
moved or deleted.
"""

from __future__ import annotations

import copy
import logging
import threading

from PyQt6.QtWidgets import QDialog, QMessageBox

from ...core import gamefinder
from ...core import process as process_core
from ...core import relink as relink_core
from ...core.backends import create as create_backend
from ...core.config import Config
from ..found_copies import FoundCopiesDialog

log = logging.getLogger("nadare.gui.main_window")


class FoundCopiesMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _start_found_copies_check(self) -> None:
        """Look for games whose files are elsewhere, off the GUI thread."""
        from ..add_game import game_specs

        # The worker must only read: give it its own Config over a deep copy.
        with self.config.locked():
            data = copy.deepcopy(self.config.data)
        snapshot = Config(path=self.config.path, data=data)
        specs = game_specs()

        def run() -> None:
            try:
                found = gamefinder.repairs(snapshot, specs)
            except Exception:  # noqa: BLE001 - a start-up check must never die
                log.warning("could not look for games in other folders", exc_info=True)
                return
            try:
                self.found_copies_ready.emit(found)
            except RuntimeError:
                pass  # the window closed before the check finished

        threading.Thread(target=run, name="nadare-found-copies", daemon=True).start()

    def _show_found_copies(self, found: object) -> None:
        """Remember and show what the start-up check found."""
        ignored = getattr(self, "_found_copies_ignored", set())
        entries = found.items() if isinstance(found, dict) else ()
        self._found_copies = {
            str(name): list(copies)
            for name, copies in entries
            if name in self.config.profiles and name not in ignored and copies
        }
        self._render_found_offer()

    def _render_found_offer(self) -> None:
        """Refresh the banner and the open page from ``_found_copies``."""
        names = list(self._found_copies)
        if names:
            titles = ", ".join(self._game_title(name) for name in names)
            self.found_banner.show_message(
                f"Nadare found {titles} in a different folder than it remembers.", "Review"
            )
        else:
            self.found_banner.hide_banner()
        if self._current_profile:
            self.game.set_found_copy(self._found_copy_path(self._current_profile))

    def _found_copy_path(self, name: str) -> str | None:
        """The best offered folder for ``name``, for the game page."""
        best = gamefinder.best(self._found_copies.get(name, []))
        return str(best.game_dir) if best is not None else None

    def _review_found_copies(self) -> None:
        """Open the first offered game and ask which folder to use."""
        if not self._found_copies:
            return
        self._guarded(self._review_found_copy_now)

    def _review_found_copy_now(self) -> None:
        name = next(iter(self._found_copies))
        self.open_game(name)
        self._use_found_copy(name)

    def _choose_found_copy(self, name: str, copies: list[gamefinder.Copy]):
        """Run the real chooser; tests replace this with a stub."""
        dialog = FoundCopiesDialog(self._game_title(name), copies, self)
        result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice

    def _use_found_copy(self, name: str) -> None:
        """Re-point ``name`` at the copy the user chose, after one confirmation."""
        copies = self._found_copies.get(name)
        if not copies:
            return
        choice = self._choose_found_copy(name, copies)
        if choice is None:
            return
        if choice == "ignore":
            self._forget_found_copy(name)
            return
        if name not in self.config.profiles:
            return

        lock = process_core.OperationLock(name)
        try:
            lock.acquire()
        except process_core.ProfileLockError:
            QMessageBox.warning(
                self, "Use this folder", process_core.OPERATION_BUSY.format(name=name)
            )
            return
        try:
            known = self._snapshot(name).get("status")
            problem = gamefinder.running_problem(
                lambda: create_backend(name, self.config).status(), known
            )
            if problem is not None:
                # The page already shows it is running; a modal box here would only repeat it.
                self._set_side_status(problem)
                if name == self._current_profile:
                    self.game.fail(problem, quiet=True)
                return
            try:
                gamefinder.use_and_save(self.config, name, choice)
            except relink_core.RelinkError as exc:
                QMessageBox.warning(self, "Use this folder", str(exc))
                return
            except OSError as exc:
                QMessageBox.critical(
                    self, "Use this folder", f"Could not save config: {exc}"
                )
                return
        finally:
            lock.release()

        self._found_copies.pop(name, None)
        self._found_copies_ignored.add(name)
        self._render_found_offer()
        self.refresh_library()
        self._start(name, "status", silent=True)

    def _on_games_repaired(self, names: object) -> None:
        """Find games re-pointed profiles at a found folder: drop this session's offers."""
        for name in names if isinstance(names, list) else ():
            key = str(name)
            self._found_copies.pop(key, None)
            self._found_copies_ignored.add(key)
        self._render_found_offer()
        self.refresh_library()

    def _forget_found_copy(self, name: str) -> None:
        """Drop this session's offer for ``name`` (Not now)."""
        self._found_copies.pop(name, None)
        self._found_copies_ignored.add(name)
        self._render_found_offer()
