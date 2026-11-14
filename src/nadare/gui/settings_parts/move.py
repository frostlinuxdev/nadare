# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Moving a game's files or its prefix to another folder.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from ...core import importmove
from ...core import process as process_core
from ...core import relink as relink_core
from ...core import relocate as relocate_core
from ...core.backends import BackendError
from ...core.backends import create as create_backend
from ...core.config import Config
from ..library_picker import resolve_other_folder
from ..workers import CallTask


class _MoveSignals(QObject):
    progress = pyqtSignal(object, object)  # bytes done, bytes total


class MoveMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    def show_move_section(self) -> None:
        """Switch to the tab that holds the Move button."""
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).isAncestorOf(self.move_button):
                self.tabs.setCurrentIndex(index)
                return

    def _make_move_button(self) -> QPushButton:
        button = QPushButton("Move to another folder…")
        button.setAutoDefault(False)
        button.setVisible(False)
        button.clicked.connect(self._move_prefix)
        return button

    def _managed_home(self) -> Path | None:
        config, name = self._config, self._profile
        data = config.profiles.get(name) if config is not None and name else None
        return relink_core.managed_home(data) if isinstance(data, dict) else None

    def _move_label(self) -> str:
        return "Move game and prefix…" if self._managed_home() else "Move prefix only…"

    def _move_tip(self) -> str:
        if self._managed_home():
            return (
                "Move the game folder and its Wine prefix together into a library folder you "
                "pick. Nadare keeps your original if it has to copy to another disk."
            )
        return (
            "Move only the Wine prefix to another folder; the game folder stays where it is. "
            "Nadare keeps your original if it has to copy to another disk."
        )

    def _move_prefix(self) -> None:
        """Move the prefix (and the game folder inside it) to a folder the user picks."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        if not self._game_idle or self._moving:
            return
        self._moving = True  # also covers the dialogs below: a second click must not start a move
        self.move_button.setEnabled(False)
        if not self._ask_and_run_move(config, name):
            self._moving = False
            self.move_button.setEnabled(self._game_idle)

    def _ask_and_run_move(self, config: Config, name: str) -> bool:
        """Check, ask, then start the move; True once it is running."""
        data = config.profiles[name]
        if not data.get("prefix"):
            where = (
                f"Nadare is using its own folder, {self._probed_prefix}. "
                if self._probed_prefix
                else ""
            )
            QMessageBox.warning(
                self,
                "Wine prefix",
                "This game has no prefix set, so there is nothing to move. "
                f"{where}Use \"I moved it myself\" and pick that folder to give the game a "
                "location of its own, then Move will work.",
            )
            return False
        old = Path(str(data.get("prefix"))).expanduser()
        if not old.is_dir():
            QMessageBox.warning(
                self,
                "Wine prefix",
                f"{old} was not found. If you moved it yourself, use "
                "\"I moved it myself\" instead.",
            )
            return False
        try:
            relink_core.check_movable(data, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Wine prefix", str(exc))
            return False
        home = relink_core.managed_home(data)
        if home is not None:
            return self._ask_and_move_home(config, name, home)
        parent = QFileDialog.getExistingDirectory(
            self, "Move the Wine prefix into which folder?", str(old.parent)
        )
        if not parent:
            return False
        if Path(parent).resolve() == old.parent.resolve():
            QMessageBox.warning(
                self,
                "Wine prefix",
                "The prefix is already in that folder. Pick a different folder.",
            )
            return False
        dst = relink_core.prefix_destination(data, old, parent)
        try:
            relink_core.check_place(dst)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Wine prefix", str(exc))
            return False
        message = (
            f"Move {old} to {dst}?\n\nOn the same disk this is instant. On another disk "
            "Nadare copies it, checks the copy, and keeps your original, so you can delete "
            "it yourself when you are happy. Nothing is deleted."
        )
        install = str(data.get("install_dir") or "")
        if install and not relink_core.is_inside(Path(install).expanduser(), old):
            message += f"\n\nOnly the Wine prefix moves; the game folder stays at {install}."
        answer = QMessageBox.question(self, "Move the Wine prefix?", message)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._run_move(config, name, old, dst)
        return True

    def _ask_and_move_home(self, config: Config, name: str, home: Path) -> bool:
        """Move a game Nadare laid out as ``<library>/Nadare/<Game>`` as one unit."""
        data = config.profiles[name]
        title = data.get("title") or name
        library = self._choose_library(
            config, f"Move {title} into which library?", str(home.parent.parent)
        )
        if not library:
            return False
        # Picking the library's own "Nadare" folder means the library: otherwise the move
        # would make "Nadare/Nadare/<Game>" again (#1293).
        library = str(resolve_other_folder(library))
        try:
            relink_core.check_home_target(data, library, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Move the game", str(exc))
            return False
        dst = relink_core.free_home(library, relink_core.home_name(data, home), config.profiles)
        install = str(data.get("install_dir") or "")
        install_exists = bool(install) and Path(install).expanduser().is_dir()
        text = (
            f"Move {home} to {dst}?\n\nThe game folder and its Wine prefix move together. "
            "On the same disk this is instant. On another disk Nadare copies them, checks the "
            "copy, and keeps your original, so you can delete it yourself when you are happy. "
            "Nothing is deleted."
        )
        if install and not install_exists:
            text += (
                "\n\nThe game folder does not exist yet (the game's own launcher installs it), "
                f"so only the Wine prefix is copied now; the game will install into {dst}."
            )
        answer = QMessageBox.question(self, "Move the game?", text)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._run_move(config, name, home, dst, managed=True, install_existed=install_exists)
        return True

    def _run_move(
        self,
        config: Config,
        name: str,
        old: Path,
        dst: Path,
        *,
        managed: bool = False,
        install_existed: bool = True,
        import_move: importmove.ImportMove | None = None,
    ) -> None:
        from PyQt6.QtWidgets import QProgressDialog

        from ...core.events import Cancel

        if self._move_running:
            return
        self._move_running = True
        cancel = Cancel()
        signals = _MoveSignals(self)
        dialog = QProgressDialog(
            "Moving the game and prefix…" if managed else "Moving the Wine prefix…",
            "Cancel",
            0,
            100,
            self,
        )
        dialog.setWindowTitle("Moving")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancel.cancel)

        def on_progress(done: object, total: object) -> None:
            total_bytes = max(int(total), 1)  # type: ignore[call-overload]
            dialog.setValue(min(99, int(int(done) * 100 / total_bytes)))  # type: ignore[call-overload]

        signals.progress.connect(on_progress)
        self._moving = True
        self.move_button.setEnabled(False)

        # The lock outlives the worker: the profile names the old path until done() saves the
        # new one, and another window or the CLI must not work on it in between (#971).
        held: list[process_core.OperationLock] = []

        def release_held() -> None:
            while held:
                held.pop().release()

        def work() -> object:
            lock = process_core.OperationLock(name)
            try:
                lock.acquire()
            except process_core.ProfileLockError:
                raise relocate_core.RelocateError(
                    process_core.OPERATION_BUSY.format(name=name)
                ) from None
            moved = False
            try:
                # The page's idea of "not running" may be minutes old: ask again right now.
                try:
                    state = getattr(create_backend(name, config).status(), "state", "")
                except Exception as exc:  # noqa: BLE001 - unknown means do not move
                    raise relocate_core.RelocateError(
                        f"Could not check whether the game is running ({exc})."
                    ) from None
                if state == "running":
                    raise relocate_core.RelocateError(
                        "The game is running. Close it first."
                    )
                if import_move is not None:
                    results = importmove.run(
                        import_move, cancel=cancel,
                        progress=lambda d, t: signals.progress.emit(d, t),
                    )
                    result = next((r for r in results if r.kept_old is not None), results[-1])
                else:
                    result = relocate_core.move_tree(
                        old, dst, cancel=cancel,
                        progress=lambda d, t: signals.progress.emit(d, t),
                    )
                moved = True
                held.append(lock)
                return result
            finally:
                if not moved:
                    lock.release()

        task = CallTask(work)

        def finish() -> bool:
            """Reset the state; False when the page is gone (nothing on it may be touched)."""
            self._moving = False
            self._move_running = False
            if sip.isdeleted(self):
                return False
            dialog.canceled.disconnect()  # closing the dialog would otherwise count as Cancel
            dialog.close()
            dialog.deleteLater()
            signals.deleteLater()
            self.move_button.setEnabled(self._game_idle)
            return True

        def done(result: object) -> None:
            try:
                _done(result)
            finally:
                release_held()

        def _done(result: object) -> None:
            alive = finish()
            parent = self if alive else None
            if name not in config.profiles:
                QMessageBox.warning(
                    parent,
                    "Wine prefix",
                    f"The game was removed while it moved. Its folder is now {dst}.",
                )
                return
            stuck = (
                f"Nadare moved the folder to {dst} but could not update the game. Use "
                "\"I moved it myself\" in the game's Settings and pick "
                f"{dst / 'prefix' if managed or import_move is not None else dst}."
            )
            try:
                if import_move is not None:
                    plan = importmove.relink_plan(config.profiles[name], import_move)
                elif managed:
                    plan = relink_core.plan_home_move(
                        config.profiles[name], dst, install_existed=install_existed
                    )
                else:
                    plan = relink_core.plan_relink(
                        config.profiles[name], dst, made_by_nadare=True
                    )
            except relink_core.RelinkError as exc:
                QMessageBox.warning(parent, "Wine prefix", f"{exc}\n\n{stuck}")
                return
            if not self._commit_relink(config, name, plan):
                QMessageBox.warning(parent, "Wine prefix", stuck)
                return
            if not alive:
                return
            if config is self._config and name == self._profile:
                self._show_placement()
            kept = getattr(result, "kept_old", None)
            if kept is not None:
                QMessageBox.information(
                    self,
                    "Moved",
                    f"Now using {dst}.\n\nYour original is still at {kept}. Delete it "
                    "yourself when you are happy with the new one; Nadare will not.",
                )
            self.note.setText(f"Moved to {dst}")

        def failed(message: str) -> None:
            release_held()  # normally already released by the worker
            if not finish():
                return
            if cancel.cancelled:
                self.note.setText("Move cancelled; nothing was changed.")
                return
            QMessageBox.warning(
                self, "Wine prefix", f"{message}\n\nNothing was changed."
            )

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

    def _free_hint(self, path: str) -> str:
        text = path.strip()
        if not text:
            return ""
        try:
            from ...core import diskspace

            free = diskspace.human_size(diskspace.free_bytes(text))
            return f"{free} free on {diskspace.mount_point(text)}"
        except (OSError, BackendError):
            return ""

    def _update_free_hints(self) -> None:
        self.install_dir_free.setText(self._free_hint(self.install_dir.text()))
        for key, label in self._option_free.items():
            editor = self._option_widgets.get(key)
            if isinstance(editor, QLineEdit):
                label.setText(self._free_hint(editor.text()))
