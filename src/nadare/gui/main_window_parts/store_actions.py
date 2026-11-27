# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What the Store page asks the window to do: play, install, add a game.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    diskspace,
    paths,
    steamstore,
)
from ...core.storecatalog import game_of
from ..add_game import PREFIX_ONLY_KINDS, AddGameDialog
from ..quick_install import VARIANT_CHANGED, QuickInstallDialog, facts_for

log = logging.getLogger("nadare.gui.main_window")


class StoreActionsMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _store_play(self, kind: str) -> None:
        """Store Play: open the page of the game already installed for ``kind``.

        The page's own Launch button then applies every check the Library does (an install
        in progress, login, notices). With several copies the first one opens.
        """
        for name, data in self.config.profiles.items():
            if kind in steamstore.installed_kinds({name: data}):
                self.open_game(name)
                return

    def _ask_steam_tile(self, title: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Add from Steam")
        box.setText(
            f"Add {title} to your library?\n\nThe game itself comes from your own Steam. "
            "Next, Nadare opens a checklist: Steam installs the game, and Nadare fixes what "
            "needs fixing, one step at a time."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def _store_steam_install(self, kind: str) -> None:
        """A Steam tile: add (or reopen) its steam profile, then show the Set up checklist."""
        fields = steamstore.profile_for(kind)
        if fields is None:
            return
        name = next(
            (n for n, data in self.config.profiles.items()
             if kind in steamstore.installed_kinds({n: data})),
            "",
        )
        if not name:
            title = steamstore.default_name(kind)
            if not self._ask_steam_tile(title):
                return
            name, number = title, 2
            while name in self.config.profiles:
                name, number = f"{title} {number}", number + 1
            try:
                with self.config.locked():
                    self.config.add_profile(name, title=title, **fields)
                    self.config.save()
            except OSError as exc:
                QMessageBox.warning(self, "Add from Steam", f"Could not save the game: {exc}")
                self.config.profiles.pop(name, None)
                return
            self.refresh_library()
        self.open_game(name)
        self._open_steam_setup()

    def _store_install(self, kind: str) -> None:
        """Store Install: one confirm, then create the profile and start the install.

        Uses the same defaults, validation and paths as the Add game dialog (it is built
        but never shown); the full dialog stays behind Advanced and the sidebar. Picking
        another version in the dialog asks again for that version.
        """
        while kind:
            kind = self._store_install_once(kind)

    def _store_install_once(self, kind: str) -> str:
        """One round of :meth:`_store_install`; returns another version to ask for, or ""."""
        if steamstore.app_id_of(kind) is not None:
            self._store_steam_install(kind)
            return ""
        existing = core_backends.existing_profile(kind, self.config.profiles)
        if existing:
            self.open_game(existing)
            return ""
        helper = AddGameDialog(self.config, self, kind=kind)
        try:
            game = helper._selected_game()
            if game is None or helper.selected_kind() != kind:
                self._add_game_dialog(kind=kind)  # not a plain install: use the full dialog
                return ""
            assume = bool(os.environ.get("NADARE_ASSUME_NOTICES"))
            show_notice = (
                core_backends.unofficial_notice(kind) and not assume
                and not self._unofficial_accepted()
            )
            notice = self._account_notice_for_kind(kind)
            size = core_backends.download_size_hint(kind)
            space_need = core_backends.install_space_hint(kind)
            asked = core_backends.install_choice(kind)  # e.g. WuWa's quality (#990)
            sizes = asked[1] if asked else {}
            if asked and sizes.get(str(asked[0].default)):
                size = sizes[str(asked[0].default)]
                space_need = size  # the quality picked decides what must fit too

            def pick(value: str) -> None:
                nonlocal size, space_need
                size = sizes.get(value, size)
                space_need = size

            def describe(chosen: str | None) -> tuple[str, str, str]:
                # Shows what create_profile will really store (the picked folder is a library).
                if not chosen:
                    helper.use_default_location()  # the default library (again)
                elif kind in PREFIX_ONLY_KINDS:
                    helper.set_prefix(chosen)  # the whole install is the prefix
                elif chosen:
                    helper.set_install_dir(chosen)
                install_dir, prefix, _owned, _dirs = helper._plan_paths(
                    game, helper.folder.text().strip(), helper.prefix.text().strip()
                )
                where = (prefix if kind in PREFIX_ONLY_KINDS else install_dir) or str(
                    paths.profiles_dir()
                )
                try:
                    free = f"{diskspace.human_size(diskspace.free_bytes(where))} free"
                except (OSError, core_backends.BackendError):
                    free = "free space unknown"
                summary = ""
                if helper.reused is not None:
                    summary = (
                        f"{game.title} is already in this library. Nadare uses those files "
                        "and the install fetches only what is missing or changed."
                    )
                elif size:
                    need = space_need or size
                    if need > size and diskspace.human_size(need) != diskspace.human_size(size):
                        summary = (
                            f"Installing {game.title} downloads about "
                            f"{diskspace.human_size(size)} and needs about "
                            f"{diskspace.human_size(diskspace.free_needed(need))} free space "
                            f"({free})."
                        )
                    else:
                        summary = (
                            f"Installing {game.title} downloads about "
                            f"{diskspace.human_size(size)} and needs about as much free "
                            f"space ({free})."
                        )
                return where, free, summary

            dialog = None
            choice_value = None
            if not assume or notice:
                group = game_of(kind)
                builds = group.entries if group is not None else ()
                dialog = QuickInstallDialog(
                    game.title,
                    notice=core_backends.UNOFFICIAL_NOTICE if show_notice else "",
                    describe=describe,
                    account_notice=notice or "",
                    parent=self,
                    choice=asked[0] if asked else None,
                    choice_sizes=sizes,
                    on_choice=pick,
                    libraries=self._library_choices(),
                    # What the Store detail page used to show, now before Install (#1536).
                    facts=facts_for(kind),
                    variants=[(build.variant or build.title, build.kind) for build in builds],
                    current_kind=kind,
                )
                outcome = dialog.exec()
                accepted = outcome == QDialog.DialogCode.Accepted
                choice_value = dialog.choice_value
                other_build = dialog.variant_kind if outcome == VARIANT_CHANGED else ""
                dialog.deleteLater()
                if other_build and other_build != kind:
                    # Another version of the game: its size and folder differ, so ask afresh.
                    return other_build
                if not accepted:
                    return ""
            profile = helper.create_profile()
            if not profile:
                QMessageBox.warning(
                    self, "Add game", helper.error_label.text() or "Could not add the game."
                )
                return ""
        finally:
            helper.deleteLater()
        if asked and choice_value is not None:
            try:
                with self.config.locked():
                    self.config.profiles[profile][asked[0].key] = choice_value
                    self.config.save()
            except OSError:
                log.debug("could not store the install choice", exc_info=True)
        if notice and not self._remember_account_notice(profile):
            self.refresh_library()
            self.open_game(profile)
            return ""
        if show_notice:
            self._remember_unofficial()
        self.refresh_library()
        self.open_game(profile)
        self._start(profile, "install")
        return ""

    def _remember_unofficial(self) -> None:
        """Remember that the one-time unofficial notice was accepted, once for the app."""
        previous = self.config.data.get("unofficial_notice_ack")
        try:
            with self.config.locked():
                self.config.data["unofficial_notice_ack"] = True
                self.config.save()
        except OSError as exc:
            if previous is None:
                self.config.data.pop("unofficial_notice_ack", None)
            else:
                self.config.data["unofficial_notice_ack"] = previous
            QMessageBox.warning(
                self, "Nadare", f"Could not remember your answer ({exc}); you will be asked again."
            )

    @staticmethod
    def _account_notice_for_kind(kind: str) -> str | None:
        try:
            notice = getattr(core_backends.get(kind), "account_notice", None)
        except core_backends.BackendError:
            return None
        return str(notice) if notice else None

    def _remember_account_notice(self, profile: str) -> bool:
        data = self.config.profiles.get(profile)
        if not data:
            return False
        try:
            with self.config.locked():
                data["accept_account_notice"] = True
                self.config.save()
        except OSError as exc:
            data["accept_account_notice"] = False
            QMessageBox.warning(self, "Account notice", f"Could not save config: {exc}")
            return False
        return True

    def _add_game_dialog(self, *, kind: str | None = None) -> None:
        dialog = AddGameDialog(self.config, self, kind=kind)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.created_profile
        if not profile or profile not in self.config.profiles:
            return
        self.refresh_library()
        self.open_game(profile)
