# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Remember login and Sandbox switches.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

from ...core.gamefacts import SandboxFacts
from .shared import (
    _SANDBOX_TIP,
)


class SwitchesMixin:
    """Mixin for :class:`GameView`; see the module text."""

    def set_account_controls(self, supported: bool) -> None:
        """Show account controls only for backends that log in."""
        self.login.setVisible(supported)
        self.logout.setVisible(supported)
        self._refresh_actions()

    def set_remember(self, supported: bool, checked: bool) -> None:
        """Show Remember me (page + QR card) for backends that support it."""
        for box in (self.remember, self.qr_frame.remember):
            box.blockSignals(True)
            box.setVisible(bool(supported))
            box.setChecked(bool(checked))
            box.blockSignals(False)

    def _on_remember_clicked(self, checked: bool) -> None:
        # Keep the page and QR-card boxes in step, then tell the window.
        for box in (self.remember, self.qr_frame.remember):
            box.blockSignals(True)
            box.setChecked(bool(checked))
            box.blockSignals(False)
        self.remember_toggled.emit(bool(checked))

    def set_sandbox(
        self, supported: bool, checked: bool, blocked: str = "", *, ready: bool = True
    ) -> None:
        """Show the Sandbox switch for games that offer it; ``blocked`` greys it out with why.

        ``ready`` is False when no bubblewrap exists here: the switch keeps the choice, the pill
        does not claim protection the next launch cannot give.
        """
        self.sandbox.blockSignals(True)
        self.sandbox.setVisible(bool(supported))
        self.sandbox.setChecked(bool(checked))
        self._sandbox_supported = bool(supported)
        self._sandbox_blocked = blocked
        self._sandbox_ready = bool(ready)
        self.sandbox.blockSignals(False)
        self._apply_sandbox()

    def _sandbox_facts(self) -> SandboxFacts:
        return SandboxFacts(
            supported=self._sandbox_supported,
            chosen=self.sandbox.isChecked(),
            # A game already running when the page opened counts too (final round).
            running=self._busy_op == "launch"
            or bool(self._status and (self._status.running or self._status.state == "running")),
            active=self._sandbox_active,
            blocked=self._sandbox_blocked,
            ready=self._sandbox_ready,
        )

    def _apply_sandbox(self) -> None:
        """Draw the switch and the pill from the facts: the saved switch is not the running game."""
        facts = self._sandbox_facts()
        self.sandbox.setEnabled(facts.editable())
        self.sandbox.setToolTip(facts.tooltip(_SANDBOX_TIP))
        pill = facts.pill()
        self.sandbox_pill.setText(pill or "SANDBOX")
        self.sandbox_pill.setVisible(bool(pill))

    def _apply_sandbox_lock(self) -> None:
        self._apply_sandbox()

    def _on_sandbox_clicked(self, checked: bool) -> None:
        self._apply_sandbox()
        self.sandbox_toggled.emit(bool(checked))
