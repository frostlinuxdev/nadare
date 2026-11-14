# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The prefix lock, old prefix backups and re-linking a prefix.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from ...core import gamefinder
from ...core import libraries as core_libraries
from ...core import process as process_core
from ...core import relink as relink_core
from ...core.backends import create as create_backend
from ...core.config import Config
from ..found_copies import FoundCopiesDialog
from ..library_picker import resolve_other_folder
from ..workers import CallTask


class PrefixToolsMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    def _refresh_prefix_lock(self, has_wine_prefix: bool) -> None:
        """Lock the prefix now, then unlock it only if the game is shown not to be installed.

        The status probe can be slow (keyring, ``/proc`` scan), so it runs on the thread pool; a
        failed probe leaves the field locked, since unlocking is the risky direction.
        """
        self._prefix_lock_generation += 1
        self._game_idle = False  # unknown until this profile's probe answers
        self._game_active = False
        self._probed_prefix = ""
        if not has_wine_prefix or self._config is None or not self._profile:
            self._set_prefix_locked(False)
            return
        self._set_prefix_locked(True, "Checking whether the game is installed…")
        generation, profile, config = self._prefix_lock_generation, self._profile, self._config

        def probe() -> tuple[int, object]:
            try:
                return generation, create_backend(profile, config).status()
            except Exception:  # noqa: BLE001 - reported as "could not check", never as "free"
                return generation, None

        task = CallTask(probe)
        # A bound method, so Qt drops the connection if the page is destroyed meanwhile.
        task.signals.done.connect(self._on_prefix_probe)
        QThreadPool.globalInstance().start(task)

    def _on_prefix_probe(self, answer: object) -> None:
        """Apply a finished status probe on the GUI thread (a stale answer is dropped)."""
        generation, status = answer  # type: ignore[misc]
        if generation != self._prefix_lock_generation:
            return
        if status is None:
            self._game_idle = False
            self._game_active = True  # unknown could be running: no Change location
            self._set_prefix_locked(
                True,
                "Could not check whether the game is installed, so this location stays locked.",
            )
            return
        state = getattr(status, "state", "")
        self._game_idle = state != "running"
        self._game_active = state in ("installed", "running")
        data = getattr(status, "data", None)
        # A profile with no prefix key uses a folder of Nadare's own; the status knows where.
        self._probed_prefix = str(data.get("prefix") or "") if isinstance(data, dict) else ""
        self._set_prefix_locked(state in ("installed", "running"))

    def _set_prefix_locked(self, locked: bool, note: str | None = None) -> None:
        """Stop an installed game's prefix from being silently repointed."""
        catalog = self._in_folder_model()
        # A catalog game's folder and prefix are one location in every state (#1293): free
        # text let them drift apart. Custom Wine games keep their free paths.
        editor = self._option_widgets.get("prefix")
        if isinstance(editor, QLineEdit):
            editor.setReadOnly(locked or catalog)
        self.install_dir.setReadOnly(catalog)
        self._install_dir_browse.setEnabled(not catalog)
        browse = self._option_browse.get("prefix")
        if browse is not None:
            browse.setEnabled(not (locked or catalog))
        self.change_location_button.setVisible(catalog and not self._game_active)
        self.change_location_button.setEnabled(catalog and not self._game_active)
        if self._prefix_lock_note is not None:
            self._prefix_lock_note.setText(note or self._LOCKED_NOTE)
            self._prefix_lock_note.setVisible(locked)
        self.relink_button.setVisible(locked)
        self.relink_button.setEnabled(locked and self._game_idle)
        self.move_button.setVisible(locked)
        self.move_button.setEnabled(locked and self._game_idle and not self._moving)
        self.move_button.setText(self._move_label())
        self.move_button.setToolTip(
            "Available once Nadare has confirmed the game is not running."
            if not self._game_idle
            else self._move_tip()
        )
        self.relink_button.setToolTip(
            "Available once Nadare has confirmed the game is not running."
            if not self._game_idle
            else "Use this after moving the game's Wine prefix folder yourself. "
            "Nothing is copied or deleted."
        )

    def _choose_found_copy(self, name: str, copies: list[gamefinder.Copy]):
        """Run the real chooser; tests replace this with a stub."""
        dialog = FoundCopiesDialog(str(self._profile_title(name)), copies, self)
        result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice

    def _profile_title(self, name: str) -> str:
        data = self._config.profiles.get(name) if self._config is not None else None
        return str(data.get("title") or name) if isinstance(data, dict) else name

    def _change_location(self) -> None:
        """Change a catalog game's whole location: a found copy, a Move, or a new library."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        profile = config.profiles[name]
        if not self._in_folder_model():
            return

        from ..add_game import game_specs

        spec = next(
            (item for item in game_specs() if item.kind == profile.get("backend")), None
        )
        copies = [c for c in gamefinder.find_copies(config, name, spec) if not c.recorded]
        if copies:
            choice = self._choose_found_copy(name, copies)
            if choice is None or choice == "ignore":
                return
            if name not in config.profiles:
                return
            lock = process_core.OperationLock(name)
            try:
                lock.acquire()
            except process_core.ProfileLockError:
                QMessageBox.warning(
                    self, "Change location", process_core.OPERATION_BUSY.format(name=name)
                )
                return
            try:
                problem = gamefinder.running_problem(
                    lambda: create_backend(name, config).status()
                )
                if problem is not None:
                    QMessageBox.warning(self, "Change location", problem)
                    return
                try:
                    gamefinder.use_and_save(config, name, choice)
                except relink_core.RelinkError as exc:
                    QMessageBox.warning(self, "Change location", str(exc))
                    return
                except OSError as exc:
                    QMessageBox.critical(
                        self, "Change location", f"Could not save config: {exc}"
                    )
                    return
            finally:
                lock.release()
            self.load(config, name)
            self.saved.emit(name)
            return

        recorded = core_libraries._game_folder(profile, name, None)
        if recorded is not None and recorded.is_dir() and any(recorded.iterdir()):
            # A partial download is there: only Move can keep it and its prefix together.
            self._move_prefix()
            return

        title = str(profile.get("title") or name)
        roots = core_libraries.library_roots(config)
        start = str(roots[1] if len(roots) > 1 else (recorded.parent if recorded else Path.home()))
        library = self._choose_library(config, f"Install {title} into which library?", start)
        if library is None:
            return
        library_path = resolve_other_folder(library)
        try:
            relink_core.check_library_target(library_path)
            relink_core.check_place(library_path, library=True)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Change location", str(exc))
            return
        home = relink_core.free_home(
            library_path, core_libraries.game_folder_name(title), config.profiles
        )
        backup = copy.deepcopy(profile)
        libraries_backup = copy.deepcopy(config.data.get(core_libraries.CONFIG_KEY))
        try:
            with config.locked():
                if profile.get("install_dir") not in (None, ""):
                    profile["install_dir"] = str(home / "install")
                profile["prefix"] = str(home / "prefix")
                profile["owned_dirs"] = [str(home)]
                trees = profile.get("owned_trees")
                if isinstance(trees, list):
                    made = {"install_dir": home / "install", "prefix": home / "prefix"}
                    profile["owned_paths"] = {
                        key: str(made[key]) for key in trees if key in made
                    }
                try:
                    core_libraries.add_library(config, library_path)
                except core_libraries.LibraryError:
                    pass
                config.save()
        except OSError as exc:
            with config.locked():
                profile.clear()
                profile.update(backup)
                if libraries_backup is None:
                    config.data.pop(core_libraries.CONFIG_KEY, None)
                else:
                    config.data[core_libraries.CONFIG_KEY] = libraries_backup
            QMessageBox.critical(self, "Change location", f"Could not save config: {exc}")
            return
        self.load(config, name)
        self.saved.emit(name)

    def _first_deletable_backup(self, name: str) -> Path | None:
        """The first recorded backup the removal dialog would also let the user delete."""
        if self._config is None or name not in self._config.profiles:
            return None
        from ...core import removal

        for tree in removal.removable_trees(self._config, name):
            if tree.key.startswith("prefix_backup") and tree.safe:
                return tree.path
        return None

    def _refresh_backup_row(self, data: dict[str, Any] | None = None) -> None:
        """Show the first recorded rebuild backup that can really be deleted, if any."""
        if self._backup_label is None or self._backup_button is None:
            return
        found = self._first_deletable_backup(self._profile)
        self._backup_label.setText(f"Old prefix backup: {found}" if found else "")
        self._backup_label.setVisible(found is not None)
        self._backup_button.setVisible(found is not None)

    def _delete_old_backup(self) -> None:
        """Delete one recorded old prefix, after confirmation, from the prefix field."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        target = self._first_deletable_backup(name)
        if target is None:
            self._refresh_backup_row()
            return
        answer = QMessageBox.question(
            self,
            "Delete old backup",
            f"Permanently delete the old prefix backup at {target}?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from ...core import prefixmove

        try:
            prefixmove.delete_backup(config, name, target)
        except (prefixmove.PrefixMoveError, OSError) as exc:
            QMessageBox.warning(self, "Delete old backup", str(exc))
            return
        self.note.setText(f"Deleted old prefix backup {target}")
        self._refresh_backup_row()

    def _make_relink_button(self) -> QPushButton:
        button = QPushButton("I moved it myself — use the new location…")
        button.setAutoDefault(False)
        button.setVisible(False)
        button.clicked.connect(self._relink_prefix)
        return button

    def _relink_prefix(self) -> None:
        """Point the profile at a prefix the user moved by hand (nothing is copied)."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles or not self._game_idle:
            return
        data = config.profiles[name]
        start = str(data.get("prefix") or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select the moved Wine prefix", start)
        if not chosen:
            return
        try:
            plan = relink_core.plan_relink(data, chosen, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Wine prefix", str(exc))
            return
        message = (
            f"Nadare will use {plan.changes['prefix']} as this game's Wine prefix from now on. "
            "Nothing is copied, moved or deleted."
        )
        details = [*plan.notes, *plan.warnings]
        if details:
            message += "\n\n" + "\n".join(details)
        answer = QMessageBox.question(self, "Use the new location?", message)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if plan.sibling_install:
            follow = QMessageBox.question(
                self,
                "Game folder",
                f"The game folder seems to be at {plan.sibling_install} now. "
                "Use it as this game's folder too?",
            )
            if follow == QMessageBox.StandardButton.Yes:
                plan = relink_core.follow_install(data, plan, plan.sibling_install)
        # The page's idea of "not running" may be minutes old, or the game may have been started
        # outside Nadare since: ask once more, off the GUI thread, and only then save.
        self.relink_button.setEnabled(False)
        self._relink_ticket += 1
        ticket = self._relink_ticket
        self._pending_relink[ticket] = (config, name, plan, chosen)

        def probe() -> object:
            try:
                return ticket, getattr(create_backend(name, config).status(), "state", "")
            except Exception:  # noqa: BLE001 - unknown means do not relink
                return ticket, None

        task = CallTask(probe)
        # A bound method, so Qt drops the connection if the page is destroyed meanwhile.
        task.signals.done.connect(self._on_relink_probe)
        QThreadPool.globalInstance().start(task)

    def _on_relink_probe(self, answer: object) -> None:
        """Finish a relink once the game's state is known right now."""
        ticket, state = answer if isinstance(answer, tuple) else (None, answer)
        pending = self._pending_relink.pop(ticket, None)
        if pending is None:
            return
        config, name, plan, chosen = pending
        if state is None:
            self.relink_button.setEnabled(self._game_idle)
            QMessageBox.warning(
                self,
                "Wine prefix",
                "Could not check whether the game is running, so Nadare did not change where "
                "its Wine prefix is.",
            )
            return
        on_page = config is self._config and name == self._profile
        if state == "running":
            if on_page:
                self._game_idle = False
                self._game_active = True
                self._set_prefix_locked(True)  # the buttons follow: both stay off while it runs
            QMessageBox.warning(self, "Wine prefix", "The game is running. Close it first.")
            return
        self.relink_button.setEnabled(self._game_idle)
        lock = process_core.OperationLock(name)
        try:
            lock.acquire()
        except process_core.ProfileLockError:
            QMessageBox.warning(self, "Wine prefix", process_core.OPERATION_BUSY.format(name=name))
            return
        try:
            if self._commit_relink(config, name, plan):
                self.note.setText(f"Now using {chosen}")
        finally:
            lock.release()

    def _commit_relink(self, config: Config, name: str, plan: relink_core.Relink) -> bool:
        """Save ``plan`` into profile ``name``; False if it could not be saved.

        The profile is passed in, never read from the page: a move finishes minutes after it
        started and the page may show another game, or no page at all, by then.
        """
        if name not in config.profiles:
            return False
        alive = not sip.isdeleted(self)
        data = config.profiles[name]
        backup = copy.deepcopy(data)
        libraries_backup = copy.deepcopy(config.data.get(core_libraries.CONFIG_KEY))
        try:
            with config.locked():
                relink_core.apply(data, plan)
                # The manifest moved with the folder; refresh it and remember the new library,
                # only when the game's folder itself moved (a library the user removed must not
                # come back because a prefix was relinked).
                core_libraries.sync_game(
                    config, name, remember_library=core_libraries.folder_moved(backup, data)
                )
                config.save()
        except OSError as exc:
            with config.locked():
                data.clear()
                data.update(backup)
                if libraries_backup is None:
                    config.data.pop(core_libraries.CONFIG_KEY, None)
                else:
                    config.data[core_libraries.CONFIG_KEY] = libraries_backup
            QMessageBox.critical(
                self if alive else None, "Settings", f"Could not save config: {exc}"
            )
            return False
        if alive:
            if config is self._config and name == self._profile:
                editor = self._option_widgets.get("prefix")
                if isinstance(editor, QLineEdit):
                    editor.setText(str(plan.changes["prefix"]))
                if "install_dir" in plan.changes:
                    self.install_dir.setText(str(plan.changes["install_dir"]))
                self._loaded_state = self._form_state()
            self.saved.emit(name)
        return True
