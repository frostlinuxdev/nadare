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
    location,
    operation_policy,
    paths,
)
from ...core.config import Config
from ...core.gamefacts import primary_operation
from .. import i18n
from ..add_game import PREFIX_ONLY_KINDS
from ..notice import AccountNoticeDialog, HoldDialog
from ..quick_install import QuickInstallDialog
from .shared import (
    DOWNLOAD_CONFIRM_BYTES,
)

log = logging.getLogger("nadare.gui.main_window")

#: Operations that must not run until the account notice is acknowledged.
NOTICE_OPERATIONS = operation_policy.operations_with("gui_notice")

#: Operations before which the one-time "unofficial launcher" notice is shown.
UNOFFICIAL_OPERATIONS = frozenset({"install", "login", "launch"})


def download_folder(kind: str, data: Mapping[str, Any]) -> str:
    """The folder a fresh install's files go to, for the free-space question (#1229).

    Prefix games (CZN, FGO, launcher games such as NIKKE) keep their files in the prefix;
    the others in their game folder; without either, Nadare's own folder. A configured
    folder goes through the backend, which knows where it really installs (#W4-F8).
    """
    in_prefix = kind in PREFIX_ONLY_KINDS or kind.startswith("launcher-")
    prefix = location._setting(data, "prefix")
    install = location._setting(data, "install_dir")
    if in_prefix:
        return prefix or str(paths.profiles_dir())
    if install or prefix:
        try:
            config = Config()
            config.data["profiles"] = {"profile": {**data, "backend": kind}}
            backend = core_backends.create("profile", config)
            folder = getattr(backend, "install_dir", None)
            if callable(folder):
                return str(folder())
        except (core_backends.BackendError, OSError, TypeError, ValueError):
            log.debug("%s: could not resolve the backend install folder", kind, exc_info=True)
    return install or str(paths.profiles_dir())


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
        if operation == "launch" and not self._confirm_hold(profile):
            return False
        if operation == "install" and self._fresh_install(profile):
            # The one install sheet carries the notices itself (#1605).
            return self._confirm_download(profile)
        if operation in NOTICE_OPERATIONS and self._needs_notice(profile):
            if not self._confirm_notice(profile):
                return False
        if operation in UNOFFICIAL_OPERATIONS and not self._confirm_unofficial(profile):
            return False
        if operation == "repair" and not self._confirm_repair(profile):
            return False
        return True

    def _confirm_hold(self, profile: str) -> bool:
        """Ask before starting a game the maintainers marked dangerous (#805)."""
        from ...core import holds

        data = self.config.profiles.get(profile) or {}
        # The window thread must never wait on the network: use the memo or saved copy.
        known = holds.known()
        # One backend may carry more than one hold: ask for each, remembering every answer,
        # otherwise accepting one would only uncover the next and Play could never start.
        for _ in range(holds.MAX_HOLDS + 1):
            hold = holds.blocking_hold(
                self.config, profile, str(data.get("backend") or ""), holds=known
            )
            if hold is None:
                return True
            if HoldDialog(hold, self).exec() != QDialog.DialogCode.Accepted:
                return False
            try:
                with self.config.locked():
                    had_override = "hold_override" in data
                    previous = data.get("hold_override")
                    data["hold_override"] = holds.acknowledge(data, hold.id)
                    try:
                        self.config.save()
                    except OSError:
                        # Nothing was persisted, so the in-memory answer must not stand:
                        # a retry has to ask again instead of starting the game silently.
                        if had_override:
                            data["hold_override"] = previous
                        else:
                            data.pop("hold_override", None)
                        raise
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    i18n.tr("Launch blocked"),
                    i18n.tr("Could not save this choice: {error}", error=exc),
                )
                return False
        return False  # the list kept producing new ids: do not start unacknowledged

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

    def _unofficial_accepted(self) -> bool:
        """Whether the one-time unofficial notice was accepted (#1605: once for the app).

        Older versions asked once per game type and kept a list of kinds; an answer for
        any game counts, so such a config is rewritten to the one app-wide value.
        """
        acked = self.config.data.get("unofficial_notice_ack")
        if acked is True:
            return True
        if isinstance(acked, list) and acked:
            self._remember_unofficial()
            return True
        return False

    def _confirm_unofficial(self, profile: str) -> bool:
        """Tell the user once for the app that Nadare is unofficial."""
        kind = self._backend_kind(profile)
        if not core_backends.unofficial_notice(kind) or os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        if self._unofficial_accepted():
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
        self._remember_unofficial()
        return True

    def _fresh_install(self, profile: str) -> bool:
        """Whether ``profile`` has nothing installed yet (a fresh download, not a RESUME)."""
        status = self._snapshot(profile).get("status")
        if status is None:
            return True
        if getattr(status, "state", "") not in ("not_installed", ""):
            return False
        return not (getattr(status, "data", None) or {}).get("incomplete")

    def _confirm_download(self, profile: str) -> bool:
        """One sheet for a fresh install: size, unofficial and account notices (#1605)."""
        if not self._fresh_install(profile):
            return True  # already installed, or RESUME: only what is missing is fetched
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            # The switch covers the unofficial and download-size questions only; the
            # account notice is still the user's own acknowledgement, as it was before.
            if not self._needs_notice(profile):
                return True
            return self._confirm_notice(profile)
        kind = self._backend_kind(profile)
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
        show_unofficial = core_backends.unofficial_notice(kind) and not self._unofficial_accepted()
        notice = (self._notice_for(profile) or "") if self._needs_notice(profile) else ""
        if (
            space_error is None
            and not show_unofficial
            and not notice
            and (not size or size < DOWNLOAD_CONFIRM_BYTES)
        ):
            return True
        try:
            free = f"{diskspace.human_size(diskspace.free_bytes(where))} free"
        except (OSError, core_backends.BackendError):
            free = "free space unknown"
        summary = (
            f"Download: about {diskspace.human_size(size)}." if size else "Download size unknown."
        )
        if space_error is not None:
            summary += f"\n{space_error.message}"
        dialog = QuickInstallDialog(
            self._game_title(profile),
            notice=core_backends.UNOFFICIAL_NOTICE if show_unofficial else "",
            account_notice=notice,
            describe=lambda chosen: (where, free, summary),
            change_location=False,
            parent=self,
        )
        if space_error is not None:
            dialog.install_button.setEnabled(False)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted or space_error is not None:
            if space_error is not None:
                log.warning("%s: install refused: %s", profile, space_error.message)
                self._set_side_status(space_error.message)
            return False
        if notice and not self._remember_account_notice(profile):
            return False
        if show_unofficial:
            self._remember_unofficial()
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
