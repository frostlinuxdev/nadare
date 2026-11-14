# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The questions asked before a game action starts (unofficial build, download size, repair,
notices).

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from PyQt6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    diskspace,
    paths,
)
from ...core.config import Config
from ...core.gamefacts import primary_operation
from ..add_game import PREFIX_ONLY_KINDS
from ..notice import AccountNoticeDialog
from .shared import (
    DOWNLOAD_CONFIRM_BYTES,
)

log = logging.getLogger("nadare.gui.main_window")

#: Operations that must not run until the account notice is acknowledged.
NOTICE_OPERATIONS = frozenset({"install", "update", "launch"})

#: Operations before which the one-time "unofficial launcher" notice is shown.
UNOFFICIAL_OPERATIONS = frozenset({"install", "login", "launch"})


def download_folder(kind: str, data: Mapping[str, Any]) -> str:
    """The folder a fresh install's files go to, for the free-space question (#1229).

    Prefix games (CZN, FGO, launcher games such as NIKKE) keep their files in the prefix;
    the others in their game folder; without either, Nadare's own folder. A configured
    folder goes through the backend, which knows where it really installs (#W4-F8).
    """
    in_prefix = kind in PREFIX_ONLY_KINDS or kind.startswith("launcher-")
    if in_prefix:
        return str(data.get("prefix") or paths.profiles_dir())
    if data.get("install_dir") or data.get("prefix"):
        try:
            config = Config()
            config.data["profiles"] = {"profile": {**data, "backend": kind}}
            backend = core_backends.create("profile", config)
            folder = getattr(backend, "install_dir", None)
            if callable(folder):
                return str(folder())
        except (core_backends.BackendError, OSError, TypeError, ValueError):
            log.debug("%s: could not resolve the backend install folder", kind, exc_info=True)
    return str(data.get("install_dir") or paths.profiles_dir())


class ConfirmMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _on_game_action(self, operation: str) -> None:
        profile = self._current_profile
        if not profile:
            return
        if operation in {
            "install",
            "update",
            "repair",
            "remake_prefix",
            "uninstall",
        } and self.predownloads.is_busy(profile):
            self._show_predownload_error(
                profile, "Wait for the predownload to finish, or stop it first."
            )
            return
        if not self._confirm_action(profile, operation):
            return
        started = self._start(profile, operation)
        if not started and operation == "status" and profile == self._current_profile:
            # RETRY disabled the button expecting a status; give it back when the
            # request could not start (another operation owns the profile).
            previous = self._snapshot(profile).get("status")
            if previous is not None:
                self.game.set_status(previous)

    def _confirm_action(self, profile: str, operation: str) -> bool:
        """The notices and download questions every operation on ``profile`` asks first."""
        if operation in NOTICE_OPERATIONS and self._needs_notice(profile):
            if not self._confirm_notice(profile):
                return False
        if operation in UNOFFICIAL_OPERATIONS and not self._confirm_unofficial(profile):
            return False
        if operation == "install" and not self._confirm_download(profile):
            return False
        if operation == "repair" and not self._confirm_repair(profile):
            return False
        return True

    def _library_play(self, profile: str) -> None:
        """Card Play runs the game page's launch checks (#765); otherwise open the page."""
        if profile not in self.config.profiles:
            return
        ready = (
            profile not in self._busy
            and not self.controller.is_paused(profile)
            and primary_operation(self._snapshot(profile).get("status")) == "launch"
        )
        if not ready:
            self.open_game(profile)
            return
        if not self._confirm_action(profile, "launch"):
            return
        self._start(profile, "launch")
        self.open_game(profile)

    def _on_remake_prefix_requested(self, profile: str) -> None:
        """Confirm from Settings, return to the game page and rebuild the prefix."""
        if profile not in self.config.profiles:
            return
        if not self._confirm_remake_prefix(profile):
            return
        self.open_game(profile)
        self._start(profile, "remake_prefix")

    def _confirm_remake_prefix(self, profile: str) -> bool:
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        answer = QMessageBox.question(
            self,
            "Rebuild Wine prefix",
            f"Rebuild the Wine prefix for {self._game_title(profile)}? Nadare moves "
            "the current one aside (it is kept), creates a fresh one and sets Windows "
            "components up again. Your game files are kept. Nothing is deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ---- unofficial-launcher notice and download size ------------------------
    def _backend_kind(self, profile: str) -> str:
        return str((self.config.profiles.get(profile) or {}).get("backend") or "")

    def _confirm_unofficial(self, profile: str) -> bool:
        """Tell the user once per game type that Nadare is unofficial; remember the answer."""
        kind = self._backend_kind(profile)
        if not core_backends.unofficial_notice(kind) or os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        acked = self.config.data.get("unofficial_notice_ack")
        if isinstance(acked, list) and kind in acked:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Before you continue")
        box.setText("Nadare is an unofficial launcher")
        box.setInformativeText(core_backends.UNOFFICIAL_NOTICE)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("I understand, continue")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        try:
            with self.config.locked():
                self.config.data["unofficial_notice_ack"] = sorted({*(acked or []), kind})
                self.config.save()
        except OSError as exc:
            QMessageBox.warning(
                self, "Nadare", f"Could not remember your answer ({exc}); you will be asked again."
            )
        return True

    def _confirm_download(self, profile: str) -> bool:
        """Ask before a fresh install that downloads several GB, showing the free space."""
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        kind = self._backend_kind(profile)
        status = self._snapshot(profile).get("status")
        if status is not None and getattr(status, "state", "") not in ("not_installed", ""):
            return True  # already installed: this is a repair, not a fresh download
        if status is not None and (getattr(status, "data", None) or {}).get("incomplete"):
            return True  # RESUME: only what is missing is fetched, nothing to confirm
        data = self.config.profiles.get(profile) or {}
        size = core_backends.install_size_hint(kind)
        where = download_folder(kind, data)
        try:
            backend = core_backends.create(profile, self.config)
        except core_backends.BackendError as exc:
            log.warning("%s: install refused: %s", profile, exc.message)
            self._set_side_status(exc.message)
            return False
        space_error: core_backends.BackendError | None = None
        try:
            checked = diskspace.install_space(backend)
        except core_backends.BackendError as exc:
            # The same rule the CLI uses; the question says so, and Download cannot proceed.
            space_error = exc
            checked = None
        if checked is not None:
            size, path = checked
            where = str(path)
        if not size or size < DOWNLOAD_CONFIRM_BYTES:
            return space_error is None
        try:
            free = f"{diskspace.human_size(diskspace.free_bytes(where))} free"
        except (OSError, core_backends.BackendError):
            free = "free space unknown"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Download the game?")
        box.setText(
            f"Installing {self._game_title(profile)} downloads about "
            f"{diskspace.human_size(size)} and needs about as much free space ({free}).\n\n"
            f"It goes to: {where}"
        )
        if space_error is not None:
            box.setInformativeText(space_error.message)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.button(QMessageBox.StandardButton.Yes).setText("Download")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes or space_error is not None:
            if space_error is not None:
                log.warning("%s: install refused: %s", profile, space_error.message)
                self._set_side_status(space_error.message)
            return False
        return True

    def _confirm_repair(self, profile: str) -> bool:
        """Ask once before a repair, which may re-download a lot of data."""
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        answer = QMessageBox.question(
            self,
            "Repair files",
            f"Repair {self._game_title(profile)}? Nadare re-checks every game file "
            "and re-downloads the ones that are missing or damaged. "
            # Repair syncs to the publisher's current files (an older version is not
            # offered for download), so an update that is out is installed too (#1310 W6-5).
            "If an update is out, Repair installs it too. "
            "This can download a lot of data.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ---- account notice ----------------------------------------------------
    def _notice_for(self, profile: str) -> str | None:
        """The backend's account notice, or ``None`` when it has none."""
        data = self.config.profiles.get(profile)
        if not data:
            return None
        try:
            backend = core_backends.get(str(data.get("backend") or ""))
        except core_backends.BackendError:
            return None
        notice = getattr(backend, "account_notice", None)
        return str(notice) if notice else None

    def _needs_notice(self, profile: str) -> bool:
        data = self.config.profiles.get(profile)
        if not data or bool(data.get("accept_account_notice", False)):
            return False
        return self._notice_for(profile) is not None

    def _confirm_notice(self, profile: str) -> bool:
        """Show the modal; persist the ack only when the user continues."""
        notice = self._notice_for(profile)
        if not notice:
            return True
        dialog = AccountNoticeDialog(notice, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        data = self.config.profiles[profile]
        try:
            with self.config.locked():
                data["accept_account_notice"] = True
                self.config.save()
        except OSError as exc:
            data["accept_account_notice"] = False
            QMessageBox.critical(self, "Account notice", f"Could not save config: {exc}")
            return False
        return True
