# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The status badge, the account line and the action buttons.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

import time

from PyQt6.QtWidgets import (
    QMessageBox,
)

from ...core import steamsetup
from ...core.backends import (
    Status,
    supports_update,
)
from ...core.gamefacts import InstallFacts, display_state, primary_operation
from ..theme import badge_style, set_state_property

#: Primary button label for each operation.
PRIMARY_LABELS = {
    "install": "INSTALL",
    "update": "UPDATE",
    "login": "LOG IN",
    "launch": "LAUNCH",
    "stop": "PLAYING",
    "settings": "SET GAME FILE IN SETTINGS",
    "retry": "RETRY",
    "remove": "REMOVE THIS GAME…",
    "use_found": "USE THE COPY ON DISK…",
}


def _version_text(version: object) -> str:
    """``v``-prefixed version for the pill row; some backends already include the ``v``."""
    text = str(version or "").strip()
    if not text:
        return ""
    return text if text[0] in "vV" and text[1:2].isdigit() else f"v{text}"


class StatusMixin:
    """Mixin for :class:`GameView`; see the module text."""

    # ---- status ------------------------------------------------------------
    def set_status(self, status: Status) -> None:
        self._awaiting_status = False
        self._apply_status(status)
        data = status.data or {}
        self._predownload_version = str(data.get("predownload_version") or "")
        self._predownload_ready = bool(data.get("predownload_ready"))
        self._refresh_predownload_button()
        self._refresh_actions()
        self._apply_sandbox()  # the switch locks while the game runs
        self._render_notice()  # installed or not decides how much of it shows

    def _apply_status(self, status: Status) -> None:
        self._status = status
        shown = display_state(status)
        playing = status.running or status.state == "running"
        if playing:
            self.kicker.setText("NOW PLAYING")
        elif shown == "not_installed":
            self.kicker.setText("NOT PLAYED YET")
        else:
            self.kicker.setText("GAME")
        # Only "now playing" says something; the other two just repeat the page.
        self.kicker.setVisible(playing)
        self._set_badge(shown, status.message)
        version = _version_text(status.version)
        data = status.data or {}
        latest = _version_text(data.get("latest_version")) if data.get("update_available") else ""
        if version and latest:
            version = f"{version} · {latest} available"
        self.version_label.setText(version)
        self._mark_pending_quality(data.get("pending_quality"))
        self._set_account(status)
        self._set_detail(status.message or "")
        # The notice folds to one line once the game is installed; the snapshot status
        # arrives after set_profile has already rendered the whole notice.
        self._render_notice()

    #: Added to "Resource quality: SD" while that tier is not downloaded yet (#1335).
    _PENDING_SUFFIX = " (not downloaded yet)"

    def _mark_pending_quality(self, pending: object) -> None:
        label = getattr(self, "choice_label", None)
        if label is None:
            return
        text = label.text().removesuffix(self._PENDING_SUFFIX)
        if pending and text:
            text += self._PENDING_SUFFIX
        label.setText(text)

    def _set_account(self, status: Status) -> None:
        """Say whether the account is logged in, for backends that track it."""
        data = status.data or {}
        known = "logged_in" in data and status.state != "not_installed"
        self.account_label.setVisible(known)
        if known:
            text = "Logged in" if data["logged_in"] else "Not logged in"
            account = data.get("account")
            if data["logged_in"] and isinstance(account, str) and account:
                text += f" as {account}"
            self.account_label.setText(text)

    def _set_badge(self, state: str, message: str = "") -> None:
        pill_state, label = badge_style(state)
        self.badge.setText(label)
        set_state_property(self.badge, pill_state)
        self.badge.setToolTip(message or "")
        # An idle, installed game is "ready" by its Launch button; say it only when odd.
        self.badge.setVisible(state != "installed")

    def set_found_copy(self, path: str | None) -> None:
        """Offer ``path`` as an existing copy of the game shown on this page (#1293)."""
        self._found_copy = path or None
        if self._status is not None and self._status.state == "not_installed":
            self._set_detail(self._status.message or "")
        self._refresh_actions()

    def _found_copy_detail(self) -> str:
        if not self._found_copy:
            return ""
        status = self._status
        if status is None or status.state != "not_installed":
            return ""
        return (
            f"Nadare found this game in another folder: {self._found_copy}. "
            "Use it instead of downloading it again."
        )

    def _set_detail(self, text: str) -> None:
        found = self._found_copy_detail()
        if found:
            text = f"{found} {text}" if text else found
        data = (self._status.data if self._status else None) or {}
        leftover = data.get("leftover_files")
        if isinstance(leftover, int) and not isinstance(leftover, bool) and leftover > 0:
            hint = (
                f"{leftover} file(s) from an older version are still in the game folder. "
                "Press Repair files to remove them."
            )
            text = f"{text} {hint}" if text else hint
        self.detail_label.setText(text)
        self.detail_label.setVisible(bool(text))

    # ---- primary action ----------------------------------------------------
    def _primary_op(self) -> str | None:
        if self._busy_op is not None or self._awaiting_status:
            return None
        status = self._status
        # A copy on disk is window knowledge, not a backend status: offer it before INSTALL.
        if self._found_copy and status is not None and status.state == "not_installed":
            return "use_found"
        return primary_operation(status)

    def _refresh_actions(self) -> None:
        self._refresh_steam_setup()
        self._apply_sandbox_lock()
        self.queue_row.setVisible(self._busy_op is not None and self._queue_waiting)
        if self._busy_op is not None:
            self.pause_button.setVisible(self._can_pause())
            self.cancel_paused_button.setVisible(False)
            # A launch that is busy means the game process is up: say so and
            # offer Stop; every other busy operation is a cancellable one.
            running = self._busy_op == "launch"
            # Remove runs outside the controller: there is nothing to cancel.
            removing = self._busy_op == "remove"
            self.stop_button.setVisible(running)
            if running:
                self.primary.setText("PLAYING")
                self.primary.setObjectName("Playing")
                self.primary.setEnabled(False)
            else:
                self.primary.setText("REMOVING…" if removing else "CANCEL")
                self.primary.setObjectName("Danger")
                self.primary.setEnabled(not removing)
            self._polish(self.primary)
            for button in (
                self.login,
                self.logout,
                self.update_btn,
                self.verify,
                self.repair_btn,
            ):
                button.setEnabled(False)
            return

        self.pause_button.setVisible(False)
        self.cancel_paused_button.setVisible(self._paused_op is not None)
        op = self._primary_op()
        self.stop_button.setVisible(op == "stop" and self._paused_op is None)
        self.primary.setObjectName("Playing" if op == "stop" else "Primary")
        self._polish(self.primary)
        if self._paused_op is not None:
            self.primary.setText("RESUME")
            self.primary.setEnabled(True)
        elif op is None:
            # No status yet: a neutral, disabled skeleton, never CANCEL.
            self.primary.setText(PRIMARY_LABELS["launch"])
            self.primary.setEnabled(False)
        else:
            interrupted = op == "install" and InstallFacts.from_status(
                self._status
            ).offers_resume()
            self.primary.setText("RESUME" if interrupted else PRIMARY_LABELS.get(op, op.upper()))
            # Even "SET GAME FILE IN SETTINGS" is a real action: it opens Settings.
            # PLAYING is a state, not an action: the x next to it stops the game.
            self.primary.setEnabled(op != "stop")

        # The status before the operation is stale until the re-probe lands.
        status = None if self._awaiting_status else self._status
        install = InstallFacts.from_status(status)
        installed = install.state() == "complete"
        running = bool(status and (status.running or status.state == "running"))
        # Signing in does not need the game files: STOVE and the login page are
        # remote, and an install started later keeps the saved login.
        facts = (status.data if status else {}) or {}
        # A backend whose sign-in runs the game's own service (NIKKE) says it needs the install.
        can_login = facts.get("can_login") is not False
        paused = self._paused_op is not None
        self.login.setEnabled(bool(status) and not running and can_login and not paused)
        label = facts.get("login_label")
        self.login.setText(label if isinstance(label, str) and label else "Log in")
        self.login.setToolTip(
            "Resume or cancel the paused download first."
            if paused
            else ""
            if can_login
            else "Install the game first, then log in."
        )
        # Log out only needs an idle account: it must be reachable even when the
        # game is not installed (e.g. to drop a saved login), unless the backend
        # knows there is no sign-in to drop.
        # An expired saved login is still a saved login: Log out clears it.
        signed_out = facts.get("logged_in") is False and not facts.get("login_expired")
        self.logout.setEnabled(bool(status) and not running and not signed_out and not paused)
        self.logout.setToolTip("Resume or cancel the paused download first." if paused else "")
        can_update = supports_update(str(self._profile_data.get("backend") or ""))
        if ((status.data if status else {}) or {}).get("can_update") is False:
            can_update = False
        # Update starts its own download, which would drop the paused one's RESUME entry.
        self.update_btn.setEnabled(
            installed and not running and can_update and self._paused_op is None
        )
        self.update_btn.setToolTip(
            "Resume or cancel the paused download first."
            if can_update and self._paused_op
            else ""
            if can_update
            else "Nadare cannot update this game; update it in Steam or its own launcher."
        )
        self.verify.setEnabled(
            bool(status)
            and not running
            and status.state not in ("error", "not_installed")
            and self._paused_op is None
        )
        # Verify and Repair start their own operation, which would drop the paused download
        # (its RESUME entry), so they wait until it is resumed or cancelled (#855).
        paused_tip = "Resume or cancel the paused download first."
        self.verify.setToolTip(paused_tip if self._paused_op else self._verify_tip)
        can_repair = bool(((status.data if status else {}) or {}).get("can_repair"))
        self.repair_btn.setEnabled(self.verify.isEnabled() and can_repair)
        self.repair_btn.setToolTip(paused_tip if self._paused_op else self._repair_tip)

    def _refresh_steam_setup(self) -> None:
        """Set up is offered only for a known Steam game with no operation running."""
        self.steam_setup_btn.setVisible(self._steam_setup_available())

    def _steam_setup_available(self) -> bool:
        if self._busy_op is not None:
            return False
        status = self._status
        if status is not None and (status.running or status.state == "running"):
            return False  # the runner step closes Steam, which would end the play session
        if str(self._profile_data.get("backend") or "") != "steam":
            return False
        data = self._profile_data
        extra = data.get("extra")
        app_id = data.get("steam_app_id") or data.get("app_id")
        if not app_id and isinstance(extra, dict):
            app_id = extra.get("steam_app_id") or extra.get("app_id")
        return bool(app_id) and steamsetup.game_for(str(app_id)) is not None

    def _on_primary(self) -> None:
        if self._busy_op is not None:
            if time.monotonic() < self._primary_grace_until:
                # The first click of a double-click already started the job; the
                # button turned into CANCEL/STOP under the user's finger.
                return
            # PLAYING is disabled, so this is a CANCEL for every other operation.
            if self._busy_op != "launch":
                self.cancel_requested.emit()
            return
        if self._paused_op is not None:
            self.resume_requested.emit()
            return
        op = self._primary_op()
        if op == "retry" and self._failed_op is not None:
            self.action_requested.emit(self._failed_op)
        elif op == "retry":
            # Show CHECKING (primary disabled) until the status lands: a second
            # click used to race the first and open a modal "Busy" (#624).
            self._awaiting_status = True
            self._refresh_actions()
            self.action_requested.emit("status")
        elif op == "use_found":
            self.found_copy_requested.emit(self._profile)
        elif op == "settings":
            self.settings_requested.emit()
        elif op == "remove":
            self.remove_requested.emit(self._profile)
        elif op:
            self.action_requested.emit(op)

    def _on_stop_clicked(self) -> None:
        if time.monotonic() < self._primary_grace_until:
            return  # the second click of the double-click that started the game
        answer = QMessageBox.question(
            self,
            "Stop game",
            f"Stop {self.title.text()}? Anything you have not saved in the game may be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            # The backend's stop operation, not the generic cancel path.
            self.action_requested.emit("stop")
