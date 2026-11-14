# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The game page's Remember login and Sandbox switches.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import (
    QThreadPool,
)
from PyQt6.QtWidgets import (
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    gamesandbox,
    secrets,
)
from ...core.backends import (
    sandbox_default,
    supports_remember_me,
    supports_sandbox,
)
from ..workers import CallTask

log = logging.getLogger("nadare.gui.main_window")


class AccountsMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _on_remember_toggled(self, enabled: bool) -> None:
        """Remember me ticked on the game page or the login QR card."""
        profile = self._current_profile
        data = self.config.profiles.get(profile)
        if not data or not supports_remember_me(str(data.get("backend") or "")):
            return
        before = bool(data.get("remember_me", False))
        try:
            with self.config.locked():
                data["remember_me"] = bool(enabled)
                self.config.save()
        except OSError as exc:
            with self.config.locked():
                data["remember_me"] = before
            self.game.set_remember(True, before)
            QMessageBox.critical(self, "Remember me", f"Could not save config: {exc}")
            return
        self._sync_remember(profile)

    def _sync_sandbox(self, profile: str) -> None:
        """Show the Sandbox switch for ``profile`` as saved."""
        data = self.config.profiles.get(profile) or {}
        kind = str(data.get("backend") or "")
        supported = supports_sandbox(kind)
        ready = gamesandbox.ready_cached() if supported else False
        if ready is None:
            ready = self._sandbox_ready_answer
        if ready is None:
            # The probe starts a program: never on the GUI thread (#1207 b191). Until it answers
            # the page claims no protection (no pill); the answer re-syncs the page.
            ready = False
            self._probe_sandbox_in_background()
        self.game.set_sandbox(
            supported,
            gamesandbox.wanted(data, supported=supported, default=sandbox_default(kind)),
            ready=bool(ready),
        )

    def _on_sandbox_toggled(self, enabled: bool) -> None:
        """The Sandbox switch on the game page; the setting is the same one Settings shows."""
        profile = self._current_profile
        data = self.config.profiles.get(profile)
        kind = str((data or {}).get("backend") or "")
        if not data or not supports_sandbox(kind):
            return
        before = gamesandbox.wanted(data, supported=True, default=sandbox_default(kind))
        had_value = "secure_mode" in data
        previous = data.get("secure_mode", before)
        try:
            with self.config.locked():
                data["secure_mode"] = bool(enabled)
                self.config.save()
        except OSError as exc:
            with self.config.locked():
                if had_value:
                    data["secure_mode"] = previous
                else:
                    data.pop("secure_mode", None)
            self._sync_sandbox(profile)
            QMessageBox.critical(self, "Sandbox", f"Could not save config: {exc}")

    def _sync_remember(self, profile: str) -> None:
        """Apply the Remember me flag immediately (turning it off forgets the login)."""
        data = self.config.profiles.get(profile)
        if not data or not supports_remember_me(str(data.get("backend") or "")):
            return
        enabled = bool(data.get("remember_me", False))
        # A paused download holds the profile too: set_remember waits for it (round 6).
        if profile in self._busy or self.controller.is_paused(profile):
            # A worker owns the profile, so set_remember cannot start. Turning
            # Remember me off must still wipe the keyring, but the keyring/DBus
            # call can block, so it runs off the GUI thread.
            if not enabled and core_backends.supports_remember_me(str(data.get("backend") or "")):
                self._remember_pending.discard(profile)
                self._forget_saved_login(
                    profile, str(data.get("backend") or ""), keep_live=True
                )
            elif enabled:
                # Queue the enable until the worker frees the profile; dropping
                # it would leave the config asking for remember-me while no
                # credential is ever stored.
                self._remember_pending.add(profile)
            else:
                self._remember_pending.discard(profile)
            return
        if self._start(profile, "set_remember", enabled):
            # Watch the worker's status events so a "no keyring" notice still
            # reaches the user after the settings page has been left.
            self._remember_watching.add(profile)

    def _forget_saved_login(self, profile: str, kind: str, keep_live: bool = False) -> None:
        """Wipe the saved login off the GUI thread (keyring/DBus blocks).

        ``keep_live`` leaves a session already signed in this run alone (Remember me turned
        off), where the default also drops it (the profile is being removed).
        """

        def run() -> None:
            cls = core_backends.get(kind)
            forget = cls.forget_saved_login
            if keep_live:
                forget = getattr(cls, "forget_persisted_login", forget)
            forget(profile)

        task = CallTask(run)
        task.signals.failed.connect(lambda message: self._on_forget_failed(profile, message))
        QThreadPool.globalInstance().start(task)

    def _on_forget_failed(self, profile: str, message: str) -> None:
        """Surface a failed credential wipe instead of silently swallowing it."""
        log.warning("could not remove the saved login for %s: %s", profile, message)
        if profile == self._current_profile:
            self._set_side_status("Could not remove the saved login")

    def _show_remember_notice(self, profile: str) -> None:
        """Surface a ``set_remember`` status notice (e.g. no keyring) to the user."""
        self._remember_watching.discard(profile)
        notice = self._remember_notice.pop(profile, "")
        if notice:
            data = self.config.profiles.get(profile)
            usable = secrets.SecretStore(allow_fallback=False).secure
            if data and data.get("remember_me") and not usable:
                # Nothing can be saved, so a ticked box would only mislead.
                if self._undo_remember(profile):
                    notice += (
                        "\n\nRemember me has been turned off; "
                        "you will need to log in again each time."
                    )
                else:
                    notice += (
                        "\n\nNadare could not turn Remember me off (the settings could not be "
                        "saved); untick it on the game's page."
                    )
            QMessageBox.information(self, "Remember me", notice)

    def _undo_remember(self, profile: str) -> bool:
        """Clear the Remember me flag (and the boxes) when it cannot work; False if not saved.

        A failed save puts the flag back in memory too, so the page, the config and the
        message agree (#1207 b193).
        """
        data = self.config.profiles.get(profile)
        if not data:
            return False
        before = data.get("remember_me")
        try:
            with self.config.locked():
                data["remember_me"] = False
                self.config.save()
        except OSError:
            log.debug("could not save Remember me off", exc_info=True)
            data["remember_me"] = before
            return False
        if profile == self._current_profile:
            self.game.set_remember(True, False)
        return True

    def _apply_pending_remember(self, profile: str) -> None:
        """Apply a Remember-me change that was requested while the profile was busy."""
        if profile not in self._remember_pending:
            return
        self._remember_pending.discard(profile)
        self._sync_remember(profile)
