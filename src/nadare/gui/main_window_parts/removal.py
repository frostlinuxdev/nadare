# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Removing a game from the library.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import copy
import logging
import textwrap

from PyQt6.QtCore import (
    QThreadPool,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QGridLayout,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...core import (
    paths,
    process,
    shortcuts,
)
from ...core.config import Config
from ..workers import CallTask

log = logging.getLogger("nadare.gui.main_window")


def _removal_reason_text(reason: str) -> str:
    """Turn ``core/removal.py``'s internal reasons into plain language (#351 F-15)."""
    text = (reason or "").strip()
    fixed = {
        "is a symlink": "it is a link to another folder",
        "is a mount point": "it is a separate disk or drive",
        "is home or an ancestor of home": "it contains your home folder",
        "is inside nadare's own directories": "it is inside Nadare's own folder",
    }
    if text in fixed:
        return fixed[text]
    if text.startswith("shared with "):
        return f"another game ({text.removeprefix('shared with ')}) also uses it"
    return text or "Nadare cannot safely delete it"


class RemovalMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _on_remove_requested(self, profile: str, preselect: frozenset[str] = frozenset()) -> None:
        """Confirm, then remove ``profile`` from the library (files kept by default).

        ``preselect`` ticks those trees (``install_dir``, ``prefix``) when they are safe to delete.
        """
        if profile not in self.config.profiles:
            return
        if profile in self._busy:
            QMessageBox.information(
                self,
                "Remove game",
                f"{self._game_title(profile)} is busy. Stop it first, then remove it.",
            )
            return
        title = str(self.config.profiles[profile].get("title") or profile)
        data_dir = paths.profiles_dir() / profile
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Uninstall or remove game")
        box.setText(f"Remove {title} from Nadare?")
        box.setInformativeText(
            "This removes the game from your library, its shortcuts and its saved login. "
            "Your game files are kept unless you tick them below; to uninstall the game, "
            "tick its game folder and Wine prefix, and Nadare asks once more before deleting."
        )
        # One idea per line, the path on a line of its own: a single long line made the box
        # wider than the screen and cut the end off (b23-rc4 click-through).
        delete_data = QCheckBox(
            "Also delete Nadare's logs and settings for this game\n"
            f"{data_dir}\n"
            "(game files are only deleted if you tick them below)"
        )
        box.setCheckBox(delete_data)
        # Optional: the game's own install folder / Wine prefix, each opt-in and
        # refused (shown disabled with the reason) when it is not safe to delete.
        from ...core import removal

        tree_boxes: dict[str, QCheckBox] = {}
        extra = QWidget()
        extra_layout = QVBoxLayout(extra)
        extra_layout.setContentsMargins(0, 0, 0, 0)
        labels = {
            "install_dir": "game install folder",
            "prefix": "Wine prefix",
            "wineprefix": "Wine prefix",
            "prefix_backup": "old prefix backup",
        }
        for tree in removal.removable_trees(self.config, profile):
            name = labels.get(tree.key)
            if name is None and tree.key.startswith("prefix_backup"):
                name = "old prefix backup"
            label = f"Also delete the {name or tree.key}\n{tree.path}"
            check = QCheckBox(label)
            if not tree.safe:
                why = _removal_reason_text(tree.reason)
                check.setEnabled(False)
                check.setToolTip(f"Not deleted: {why}")
                kept = textwrap.fill(f"Not deleted, kept: {why}", 90)
                check.setText(f"{label}\n{kept}")
            elif tree.key in preselect:
                check.setChecked(True)
            tree_boxes[tree.key] = check
            extra_layout.addWidget(check)
        if tree_boxes:
            layout = box.layout()
            buttons = box.findChild(QDialogButtonBox)
            if isinstance(layout, QGridLayout) and buttons is not None:
                # Above the buttons, not under them: take the button row out and put it back last.
                index = layout.indexOf(buttons)
                _row, column, _rows, columns = layout.getItemPosition(index)
                layout.removeWidget(buttons)
                end = layout.rowCount()
                layout.addWidget(extra, end, 0, 1, layout.columnCount())
                layout.addWidget(buttons, end + 1, column, 1, columns)
            elif layout is not None:
                layout.addWidget(extra, layout.rowCount(), 0, 1, layout.columnCount())
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        confirm = box.button(QMessageBox.StandardButton.Yes)
        confirm.setText("Remove")

        def name_the_action() -> None:
            # Say what the click will do: deleting game files is an uninstall (#951).
            ticked = any(check.isChecked() for check in tree_boxes.values())
            confirm.setText("Uninstall" if ticked else "Remove")

        for check in tree_boxes.values():
            check.toggled.connect(name_the_action)
        name_the_action()  # ``preselect`` may already have ticked some
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        try:
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            chosen = [key for key, check in tree_boxes.items() if check.isChecked()]
            if chosen:
                paths_text = "\n".join(
                    str(t.path)
                    for t in removal.removable_trees(self.config, profile)
                    if t.key in chosen
                )
                answer = QMessageBox.warning(
                    self,
                    "Delete game files?",
                    "This permanently deletes these folders and everything in them:\n\n"
                    f"{paths_text}\n\nThis cannot be undone.",
                    QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._remove_game(profile, delete_data.isChecked(), chosen)
        finally:
            # Parented to the window, the box would otherwise stay alive (hidden) after the
            # dialog closes and pile up; let the event loop reap it.
            box.deleteLater()

    def _remove_game(
        self, profile: str, delete_data: bool, delete_trees: list[str] | None = None
    ) -> None:
        """Run the slow cleanup off the GUI thread, then drop the profile here."""
        from ...core import removal

        # The worker deletes files but cannot persist the profile change, so make
        # sure that final save can succeed before anything irreversible happens.
        try:
            with self.config.locked():
                self.config.save()
        except OSError as exc:
            title = self._game_title(profile)
            self._set_side_status(f"Could not remove {title}: {exc}")
            QMessageBox.critical(self, "Remove game", f"Could not save config: {exc}")
            return

        self._busy[profile] = "remove"
        if profile == self._current_profile:
            # The page says REMOVING… instead of offering its button again (round 3: a retired
            # game's REMOVE THIS GAME… stayed enabled while its own removal ran).
            self.game.set_busy("remove")
        self._set_side_status(f"Removing {self._game_title(profile)}…")

        # The worker does not read the live Config: a Settings save on this
        # thread could resize ``profiles`` while removal iterates it (#27). A
        # deep copy is taken under the lock, so the worker sees one consistent
        # snapshot and the profile itself is still dropped here.
        with self.config.locked():
            worker_config = Config(
                path=self.config.path, data=copy.deepcopy(self.config.data)
            )

        def run() -> object:
            return removal.remove_game(
                worker_config,
                profile,
                delete_data=delete_data,
                delete_trees=delete_trees or (),
                drop_profile=False,
                save=False,
                desktop_dir=shortcuts.desktop_dir(),
            )

        task = CallTask(run)
        task.signals.done.connect(lambda result: self._on_game_removed(profile, result))
        task.signals.failed.connect(lambda message: self._on_remove_failed(profile, message))
        QThreadPool.globalInstance().start(task)

    def _on_game_removed(self, profile: str, result: object) -> None:
        title = self._game_title(profile)
        self._busy.pop(profile, None)
        if self._removal_changed_nothing(result):
            # Refused up front (the game runs, another Nadare command holds it): the game is
            # exactly as it was, so a paused download and its page state stay (round 1 low).
            if profile == self._current_profile:
                self.game.fail("Not removed: nothing was deleted.", quiet=True)
            self._restore_paused_page(profile)
            self._set_side_status(f"{title} was not removed")
            self._start(profile, "status", silent=True)
            warnings = list(getattr(result, "warnings", []) or [])
            if warnings:
                QMessageBox.warning(self, "Remove game", "\n".join(warnings))
            return
        self.controller.forget(profile)
        self._forget_found_copy(profile)
        self._snapshots.pop(profile, None)
        self._status_errors.pop(profile, None)
        self._verify_failures.pop(profile, None)
        self._remember_notice.pop(profile, None)
        self._remember_watching.discard(profile)
        self._remember_pending.discard(profile)
        save_error = ""
        if not getattr(result, "profile_kept", False):
            before = dict(self.config.profiles)
            before_active = self.config.data.get("active_profile", "")
            try:
                with self.config.locked():
                    self.config.remove_profile(profile)
                    self.config.save()
            except OSError as exc:
                # Not persisted: put the profile back so memory matches disk.
                self.config.profiles.clear()
                self.config.profiles.update(before)
                self.config.data["active_profile"] = before_active
                save_error = str(exc)
                QMessageBox.critical(self, "Remove game", f"Could not save config: {exc}")
        warnings = list(getattr(result, "warnings", []) or [])
        self.refresh_library()
        self.show_library()
        kept = getattr(result, "profile_kept", False)
        if save_error:
            self._set_side_status(f"Could not remove {title}: {save_error}")
        else:
            self._set_side_status(
                f"{title} was kept (some files could not be deleted)"
                if kept
                else f"Removed {title}"
            )
        if warnings:
            QMessageBox.warning(self, "Remove game", "\n".join(warnings))

    def _on_remove_failed(self, profile: str, message: str) -> None:
        self._busy.pop(profile, None)
        if profile == self._current_profile:
            self.game.fail(message, quiet=True)
        # Only the profile-lock refusal comes before any deletion (round 6: a shortcut that
        # cannot be written fails after the trees went).
        if message == process.OPERATION_BUSY.format(name=profile):
            self._restore_paused_page(profile)
        title = self._game_title(profile)
        self._set_side_status(f"Could not remove {title}")
        self.refresh_library()
        QMessageBox.critical(self, "Remove game", f"Could not remove {title}: {message}")
