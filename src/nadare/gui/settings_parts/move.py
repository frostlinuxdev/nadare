# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Moving a game's files or its prefix to another folder.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
)

from ...core import gamefacts, importmove, location, movelog, prefixguard
from ...core import process as process_core
from ...core import relink as relink_core
from ...core import relocate as relocate_core
from ...core import removal as removal_core
from ...core.backends import BackendError
from ...core.backends import create as create_backend
from ...core.config import Config
from ..library_picker import resolve_other_folder
from ..workers import CallTask


def _record_move(config: Config, name: str, src: Path, dst: Path) -> None:
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        raise relocate_core.RelocateError("The game was removed before the Move started.")
    with config.locked():
        previous = profile.get(relocate_core.MOVE_IN_PROGRESS)
        relocate_core.begin_move(profile, src, dst)
        try:
            config.save()
        except OSError:
            if previous is None:
                profile.pop(relocate_core.MOVE_IN_PROGRESS, None)
            else:
                profile[relocate_core.MOVE_IN_PROGRESS] = previous
            raise  # a failed journal save must stop the Move


def _forget_move(config: Config, name: str) -> None:
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        return
    try:
        with config.locked():
            if relocate_core.end_move(profile):
                config.save()
    except OSError:
        pass  # the next start finds both folders as they are and forgets it


def _forget_pending(config: Config, name: str, old: Path) -> None:
    """Drop a finished (or refused) original from the profile's record and save."""
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        return
    try:
        with config.locked():
            if relocate_core.forget_pending_original(profile, old):
                config.save()
    except OSError:
        pass  # the next start prunes it once the original is gone


def remove_original_quietly(config: Config, name: str, old: Path, new: Path) -> None:
    """:func:`relocate.remove_original` in the background, with no page to report to.

    Used when the Settings page closed mid-move (review A5) and at start for originals a
    crash left behind (round 2, C4); the record goes either way, so a refused one is not
    re-checked on every start.
    """
    task = CallTask(lambda: relocate_core.remove_original(old, new))

    def done(outcome: object) -> None:
        _forget_pending(config, name, old)
        if not getattr(outcome, "removed", False) and old.exists():
            _tell_kept(old, getattr(outcome, "reason", ""))

    def failed(message: str) -> None:
        _forget_pending(config, name, old)
        if old.exists():
            _tell_kept(old, message)

    task.signals.done.connect(done)
    task.signals.failed.connect(failed)
    QThreadPool.globalInstance().start(task)


def _tell_kept(old: Path, reason: str) -> None:
    """A refused clean-up is said once, not dropped silently (storage review round 3, B-F4)."""
    from .. import notify

    notify.send(
        "An old game folder was kept",
        f"Nadare did not delete {old} after moving the game"
        + (f": {reason}" if reason else ".")
        + " Delete it yourself if you do not need it.",
    )


def _how(results: list) -> str:
    """How a move went, for its log: an instant rename or a checked copy."""
    if results and all(getattr(r, "renamed", False) for r in results):
        return "renamed (same drive)"
    return "copied and checked"


def _reveal(area: QScrollArea, button: QPushButton) -> None:
    """Scroll ``area`` so ``button`` shows, once the tab switch has laid the page out."""
    try:
        area.ensureWidgetVisible(button, 0, 48)
    except RuntimeError:
        pass  # the page was rebuilt or closed meanwhile


class _MoveSignals(QObject):
    progress = pyqtSignal(object, object)  # bytes done, bytes total


class MoveMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    def show_move_section(self) -> None:
        """Switch to the tab that holds the Move button."""
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).isAncestorOf(self.move_button):
                self.tabs.setCurrentIndex(index)
                # The button sits under the prefix field, below the fold on a laptop screen:
                # "Move game…" must land on it, not on the top of the tab (user sim, P1 #2).
                parent = self.move_button.parentWidget()
                while parent is not None and not isinstance(parent, QScrollArea):
                    parent = parent.parentWidget()
                if parent is not None:
                    QTimer.singleShot(0, lambda area=parent: _reveal(area, self.move_button))
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
                "pick. On another disk Nadare copies them, checks the copy, then deletes the "
                "original."
            )
        return (
            "Move only the Wine prefix to another folder; the game folder stays where it is. "
            + self._original_note()
        )

    def _original_note(self) -> str:
        """What happens to the original prefix on another disk (round 2, C3)."""
        config, name = self._config, self._profile
        data = config.profiles.get(name) if config is not None and name else None
        prefix = location._setting(data, "prefix") if isinstance(data, dict) else None
        if isinstance(prefix, str) and prefix.strip() and removal_core.prefix_made_by_nadare(
            data, Path(prefix).expanduser(), name
        ):
            return "On another disk Nadare copies it, checks the copy, then deletes the original."
        return (
            "On another disk Nadare copies it and checks the copy; Nadare did not make this "
            "prefix, so it keeps your original for you to delete."
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
        if not location._setting(data, "prefix"):
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
        old = Path(str(location._setting(data, "prefix"))).expanduser()
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
        # Never into another game's prefix or folder (storage review round 2, B3).
        clash = prefixguard.unsafe_reason(dst, profiles=config.profiles, own=name)
        if clash:
            QMessageBox.warning(self, "Wine prefix", f"Cannot move the prefix there: {clash}.")
            return False
        if removal_core.prefix_made_by_nadare(data, old, name):
            on_copy = (
                "Nadare copies it, checks the copy, and only then deletes the original. If "
                "the copy does not match, the original is kept."
            )
        else:
            on_copy = (
                "Nadare copies it and checks the copy. Nadare did not make this prefix, so "
                "the original is kept for you to delete."
            )
        message = (
            f"Move {old} to {dst}?\n\nOn the same disk this is instant. On another disk "
            f"{on_copy}"
        )
        install = str(location._setting(data, "install_dir") or "")
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
        install = str(location._setting(data, "install_dir") or "")
        install_exists = bool(install) and Path(install).expanduser().is_dir()
        text = (
            f"Move {home} to {dst}?\n\nThe game folder and its Wine prefix move together. "
            "On the same disk this is instant. On another disk Nadare copies them, checks the "
            "copy, and only then deletes the original. If the copy does not match, the "
            "original is kept."
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

        # QProgressDialog.setValue() runs the event loop when the dialog is modal, so done() or
        # failed() can arrive *inside* it. They then closed the dialog and opened the "Moved"
        # box from within the dialog's own setValue(): on Wayland the dialog came back as an
        # invisible modal window and the box took no input (#1754). They wait for it instead.
        in_progress: list[int] = []
        after_progress: list = []

        def outside_progress(handler):
            def run(*args: object) -> None:
                if in_progress:
                    after_progress.append(lambda: handler(*args))
                else:
                    handler(*args)

            return run

        def on_progress(done: object, total: object) -> None:
            if sip.isdeleted(dialog):  # a progress queued before the end arrives after it
                return
            total_bytes = max(int(total), 1)  # type: ignore[call-overload]
            in_progress.append(1)
            try:
                dialog.setValue(min(99, int(int(done) * 100 / total_bytes)))  # type: ignore[call-overload]
            finally:
                in_progress.pop()
            while not in_progress and after_progress:
                after_progress.pop(0)()

        signals.progress.connect(on_progress)
        self._moving = True
        self.move_button.setEnabled(False)

        # The lock outlives the worker: the profile names the old path until done() saves the
        # new one, and another window or the CLI must not work on it in between (#971).
        held: list[process_core.OperationLock] = []
        # Originals left by a copy to another disk, paired with their verified copies: removed
        # once the game points at the copies (Move moves; user, 2026-11-14), but only folders
        # Nadare made (storage review A1: never a player's own prefix, such as a shared
        # ~/.wine, nor an imported game). Kept ones are named in the result.
        originals: list[tuple[Path, Path]] = []
        kept: list[Path] = []
        how: list[str] = []  # "renamed (same drive)" or "copied and checked", for the log (#1352)
        title = str(config.profiles.get(name, {}).get("title") or name)

        def log_move(outcome: str) -> Path | None:
            return movelog.write(
                name, title=title, src=old, dst=dst, how=how[0] if how else "", outcome=outcome
            )

        if import_move is not None:
            nadare_made = False
        elif managed:
            nadare_made = True
        else:
            nadare_made = removal_core.prefix_made_by_nadare(config.profiles[name], old, name)

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
                    running = gamefacts.is_running(create_backend(name, config).status())
                except Exception as exc:  # noqa: BLE001 - unknown means do not move
                    raise relocate_core.RelocateError(
                        f"Could not check whether the game is running ({exc})."
                    ) from None
                if running:
                    raise relocate_core.RelocateError(
                        "The game is running. Close it first."
                    )
                if import_move is not None:
                    results = importmove.run(
                        import_move, cancel=cancel,
                        progress=lambda d, t: signals.progress.emit(d, t),
                    )
                    result = next((r for r in results if r.kept_old is not None), results[-1])
                    how.append(_how(results))
                    kept.extend(Path(r.kept_old) for r in results if r.kept_old is not None)
                else:
                    # A folder holding only Nadare's old manifest is cleared now, after the
                    # player confirmed, not while the question was being built (review A9).
                    _record_move(config, name, old, dst)
                    relink_core.clear_stale_folder(dst, config.profiles)
                    def save_checkpoint() -> None:
                        with config.locked():
                            config.save()
                    result = relocate_core.move_tree(
                        old, dst, cancel=cancel,
                        progress=lambda d, t: signals.progress.emit(d, t),
                        journal=config.profiles[name][relocate_core.MOVE_IN_PROGRESS],
                        checkpoint=save_checkpoint,
                    )
                    how.append(_how([result]))
                    if result.kept_old is not None:
                        if nadare_made and not cancel.cancelled:
                            originals.append((Path(result.kept_old), Path(dst)))
                        else:
                            kept.append(Path(result.kept_old))
                moved = True
                held.append(lock)
                return result
            finally:
                if not moved:
                    lock.release()

        task = CallTask(work)

        # Boxes wait for the next turn of the event loop, after done()/failed() returned: by then
        # the game is free and the progress dialog is gone. A box opened from inside done() held
        # the lock for as long as it was up, and on Wayland the closed "Moving" dialog came back
        # as a modal window over it, so it took no input and the game stayed busy (#1754).
        boxes: list = []

        def show_boxes() -> None:
            def run() -> None:
                for box in boxes:
                    box(None if sip.isdeleted(self) else self)
                boxes.clear()

            if boxes:
                QTimer.singleShot(0, run)

        def finish() -> bool:
            """Reset the state; False when the page is gone (nothing on it may be touched)."""
            self._moving = False
            self._move_running = False
            if sip.isdeleted(self):
                return False
            # Only our own slot: closing the dialog must not count as Cancel, but the dialog's
            # internal cancel handling stays (#1754).
            dialog.canceled.disconnect(cancel.cancel)
            signals.progress.disconnect(on_progress)  # a late progress must not show it again
            dialog.close()
            sip.delete(dialog)  # now, not deleteLater(): a nested loop would keep it alive
            signals.deleteLater()
            self.move_button.setEnabled(self._game_idle)
            return True

        def done(result: object) -> None:
            try:
                _done(result)
            finally:
                release_held()
                show_boxes()

        def _done(result: object) -> None:
            alive = finish()
            if name not in config.profiles:
                log_move("moved, but the game was removed while it moved")
                boxes.append(lambda parent: QMessageBox.warning(
                    parent,
                    "Wine prefix",
                    f"The game was removed while it moved. Its folder is now {dst}.",
                ))
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
                log_move(f"moved, but the game could not be updated: {exc}")
                why = f"{exc}\n\n{stuck}"
                boxes.append(lambda parent: QMessageBox.warning(parent, "Wine prefix", why))
                return
            pending: dict = dict(config.profiles[name])
            for original, copy in originals:
                changes = relocate_core.with_pending_original(plan.changes, pending, original, copy)
                pending.update(changes)
                plan = dataclasses.replace(plan, changes=changes)
            if not self._commit_relink(config, name, plan):
                log_move("moved, but the game could not be updated")
                boxes.append(lambda parent: QMessageBox.warning(parent, "Wine prefix", stuck))
                return
            log = log_move("moved")
            if import_move is not None:
                importmove.clear_journal(import_move)  # done: nothing to finish (review A6)
            else:
                _forget_move(config, name)
            if not alive:
                # The page closed during the move: the game already uses the copy, so the
                # original still goes, quietly (review A5).
                for original, copy in originals:
                    remove_original_quietly(config, name, original, copy)
                return
            if config is self._config and name == self._profile:
                self._show_placement()
            self.note.setText(f"Moved {title} to {dst}")
            for original, copy in originals:
                self._remove_original(original, copy, config, name, log=log)
            if kept:
                why = (
                    "you pressed Cancel after the copy had finished"
                    if cancel.cancelled
                    else "Nadare did not make it, so it does not delete it"
                )
                text = (
                    f"Moved {title}. Now using {dst}.\n\nThe original is still at "
                    + ", ".join(str(p) for p in kept)
                    + f" ({why}). Delete it yourself when you are happy with the new one."
                )
                boxes.append(lambda parent: QMessageBox.information(parent, "Moved", text))
            else:
                # Also an instant rename says it happened (#1352): the note alone was missed.
                said = f"Moved {title} to {dst}."
                boxes.append(lambda parent: parent is not None and self._confirm_moved(said))

        def failed(message: str) -> None:
            release_held()  # normally already released by the worker
            if import_move is None:
                _forget_move(config, name)  # nothing moved (move_tree undoes its own copy)
            log_move(
                "cancelled; nothing was changed" if cancel.cancelled else f"not moved: {message}"
            )
            if not finish():
                return
            if cancel.cancelled:
                self.note.setText("Move cancelled; nothing was changed.")
                return
            boxes.append(lambda parent: QMessageBox.warning(
                parent, "Wine prefix", f"{message}\n\nNothing was changed."
            ))
            show_boxes()

        task.signals.done.connect(outside_progress(done))
        task.signals.failed.connect(outside_progress(failed))
        QThreadPool.globalInstance().start(task)

    def _confirm_moved(self, text: str) -> None:
        """One short box after a Move; the tests replace it (see tests/conftest.py)."""
        QMessageBox.information(self, "Moved", text)

    def _remove_original(
        self, old: Path, new: Path, config: Config, name: str, *, log: Path | None = None
    ) -> None:
        """Delete the original after a verified copy, now that the game uses ``new``."""
        self.note.setText(f"Moved to {new}; removing the original at {old}...")
        task = CallTask(lambda: relocate_core.remove_original(old, new))

        def done(outcome: object) -> None:
            _forget_pending(config, name, old)
            removed = getattr(outcome, "removed", False)
            movelog.append(
                log,
                f"original removed: {old}"
                if removed
                else f"original kept: {old} ({getattr(outcome, 'reason', '') or 'unknown reason'})",
            )
            if sip.isdeleted(self):
                return
            if removed:
                self.note.setText(f"Moved to {new}. The original at {old} was removed.")
                return
            reason = getattr(outcome, "reason", "") or "unknown reason"
            self.note.setText(f"Moved to {new}. The original is still at {old}.")
            QMessageBox.information(
                self,
                "Moved",
                f"Now using {new}.\n\nThe original at {old} was not removed: {reason}",
            )

        def failed(message: str) -> None:
            _forget_pending(config, name, old)
            movelog.append(log, f"original kept: {old} ({message})")
            if not sip.isdeleted(self):
                self.note.setText(f"Moved to {new}. The original is still at {old}: {message}")

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
