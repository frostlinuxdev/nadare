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
from ..add_game import PREFIX_ONLY_KINDS, AddGameDialog
from ..notice import AccountNoticeDialog
from ..quick_install import QuickInstallDialog
from .shared import (
    DOWNLOAD_CONFIRM_BYTES,
)

log = logging.getLogger("nadare.gui.main_window")


def _ask_check_files(parent, title: str) -> bool:
    """Ask before a post-import check; No is the default so Enter never starts it."""
    answer = QMessageBox.question(
        parent,
        "Check game files",
        f"Check {title}'s game files against the official list now?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


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
        but never shown); the full dialog stays behind Advanced and the sidebar.
        """
        if steamstore.app_id_of(kind) is not None:
            self._store_steam_install(kind)
            return
        existing = core_backends.existing_profile(kind, self.config.profiles)
        if existing:
            self.open_game(existing)
            return
        helper = AddGameDialog(self.config, self, kind=kind)
        try:
            game = helper._selected_game()
            if game is None or helper.selected_kind() != kind:
                self._add_game_dialog(kind=kind)  # not a plain install: use the full dialog
                return
            assume = bool(os.environ.get("NADARE_ASSUME_NOTICES"))
            acked = self.config.data.get("unofficial_notice_ack")
            acked = acked if isinstance(acked, list) else []
            show_notice = (
                core_backends.unofficial_notice(kind) and not assume and kind not in acked
            )
            size = core_backends.install_size_hint(kind)
            asked = core_backends.install_choice(kind)  # e.g. WuWa's quality (#990)
            sizes = asked[1] if asked else {}
            if asked and sizes.get(str(asked[0].default)):
                size = sizes[str(asked[0].default)]

            def pick(value: str) -> None:
                nonlocal size
                size = sizes.get(value, size)

            def describe(chosen: str | None) -> tuple[str, str, str]:
                # Shows what create_profile will really store (the picked folder is a library).
                if not chosen:
                    helper.use_default_location()  # the default library (again)
                elif kind in PREFIX_ONLY_KINDS:
                    helper.set_prefix(chosen)  # the whole install is the prefix
                elif chosen:
                    helper.set_install_dir(chosen)
                install_dir, prefix, _owned, _dirs = helper._plan_paths(
                    game, helper.folder.text().strip(), helper.prefix.text().strip(), False
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
                elif size and size >= DOWNLOAD_CONFIRM_BYTES:
                    summary = (
                        f"Installing {game.title} downloads about "
                        f"{diskspace.human_size(size)} and needs about as much free "
                        f"space ({free})."
                    )
                return where, free, summary

            dialog = None
            choice_value = None
            if not assume:
                dialog = QuickInstallDialog(
                    game.title,
                    notice=core_backends.UNOFFICIAL_NOTICE if show_notice else "",
                    describe=describe,
                    parent=self,
                    choice=asked[0] if asked else None,
                    choice_sizes=sizes,
                    on_choice=pick,
                    libraries=self._library_choices(),
                )
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
                choice_value = dialog.choice_value
                wants_import = dialog.import_requested
                dialog.deleteLater()
                if wants_import:
                    self._add_game_dialog(kind=kind, importing=True)  # files they already have
                    return
                if not accepted:
                    return
            notice = self._account_notice_for_kind(kind)
            if notice and not self._confirm_notice_text(notice):
                return
            profile = helper.create_profile()
            if not profile:
                QMessageBox.warning(
                    self, "Add game", helper.error_label.text() or "Could not add the game."
                )
                return
        finally:
            helper.deleteLater()
        if asked and choice_value is not None:
            try:
                with self.config.locked():
                    self.config.profiles[profile][asked[0].key] = choice_value
                    self.config.save()
            except OSError:
                log.debug("could not store the install choice", exc_info=True)
        if notice:
            self._remember_account_notice(profile)
        if show_notice:
            self._remember_unofficial(kind, acked)
        self.refresh_library()
        self.open_game(profile)
        self._start(profile, "install")

    def _remember_unofficial(self, kind: str, acked: list) -> None:
        try:
            with self.config.locked():
                self.config.data["unofficial_notice_ack"] = sorted({*acked, kind})
                self.config.save()
        except OSError as exc:
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

    def _confirm_notice_text(self, notice: str) -> bool:
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        return AccountNoticeDialog(notice, self).exec() == QDialog.DialogCode.Accepted

    def _remember_account_notice(self, profile: str) -> None:
        data = self.config.profiles.get(profile)
        if not data:
            return
        try:
            with self.config.locked():
                data["accept_account_notice"] = True
                self.config.save()
        except OSError as exc:
            data["accept_account_notice"] = False
            QMessageBox.warning(self, "Account notice", f"Could not save config: {exc}")

    def _add_game_dialog(
        self, *, kind: str | None = None, advanced: bool = False, importing: bool = False
    ) -> None:
        dialog = AddGameDialog(self.config, self, kind=kind, advanced=advanced)
        if importing:
            dialog.set_mode(True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.created_profile
        if not profile or profile not in self.config.profiles:
            return
        self.refresh_library()
        self.open_game(profile)
        if dialog.created_by_import:
            self._maybe_offer_verify(profile)  # a fresh install has nothing to check yet

    def _maybe_offer_verify(self, profile: str) -> None:
        """A Yostar import with a manifest can verify right away."""
        data = self.config.profiles.get(profile)
        if not data or str(data.get("backend") or "") not in (
            "arknights-yostar",
            "stellasora-yostar",
        ):
            return
        from ...core import yostar

        if not yostar.configured_manifest(data):
            return
        if _ask_check_files(self, self._game_title(profile)):
            self._start(profile, "verify")
