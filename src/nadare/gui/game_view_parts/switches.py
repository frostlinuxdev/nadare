# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Remember login and Sandbox switches.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

from ...core import gamefacts, patchspecs
from ...core.gamefacts import SandboxFacts
from .shared import (
    _PATCH_TIP,
    _PATCH_UPDATE_TIP,
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
        self,
        supported: bool,
        checked: bool,
        blocked: str = "",
        *,
        ready: bool = True,
        locked: str = "",
    ) -> None:
        """Show the Sandbox switch for games that offer it; ``blocked`` greys it out with why.

        ``ready`` is False when no bubblewrap exists here: the switch keeps the choice, the pill
        does not claim protection the next launch cannot give. ``locked`` greys the switch out
        while the Sandbox still runs (a game that requires it).
        """
        self.sandbox.blockSignals(True)
        self.sandbox.setVisible(bool(supported))
        self.sandbox.setChecked(bool(checked))
        self._sandbox_supported = bool(supported)
        self._sandbox_blocked = blocked
        self._sandbox_locked = locked
        self._sandbox_ready = bool(ready)
        self.sandbox.blockSignals(False)
        self._apply_sandbox()

    def _sandbox_facts(self) -> SandboxFacts:
        return SandboxFacts(
            supported=self._sandbox_supported,
            chosen=self.sandbox.isChecked(),
            # A game already running when the page opened counts too (final round).
            running=self._busy_op == "launch"
            or gamefacts.is_running(self._status),
            active=self._sandbox_active,
            blocked=self._sandbox_blocked,
            ready=self._sandbox_ready,
            locked=getattr(self, "_sandbox_locked", ""),
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

    # ---- the English patch switch (#1866) --------------------------------------------
    def set_patch(self, applied: bool, *, outdated: bool = False, note: str = "") -> None:
        """Tick the switch when the patched files are in the install (the window reads them).

        ``note`` says why the patch is not active (the game moved to another build, #1894);
        Update is hidden then, since no newer copy for the old build can help.
        """
        self._patch_applied = bool(applied)
        self._patch_outdated = bool(applied and outdated and not note)
        self._patch_note = note
        self._apply_patch_switch()

    def _apply_patch_switch(self) -> None:
        """Shown for an installed game with a declared overlay; locked while busy or running."""
        status = self._status
        offered = bool(patchspecs.SPECS.get(str(self._profile_data.get("backend") or "")))
        installed = status is not None and status.state in ("installed", "update_available")
        shown = offered and installed
        self.patch_switch.setVisible(shown)
        running = gamefacts.is_running(status)
        enabled = not running and self._busy_op is None
        if running:
            tip = "The game is running: close it to turn the English patch on or off."
            update_tip = "The game is running: close it to update the English patch."
        elif self._busy_op is not None:
            tip = "Wait for the current task to finish."
            update_tip = tip
        else:
            tip = _PATCH_TIP
            update_tip = _PATCH_UPDATE_TIP
        self.patch_switch.setEnabled(enabled)
        self.patch_switch.setToolTip(tip)
        self.patch_switch.blockSignals(True)
        self.patch_switch.setChecked(self._patch_applied)
        self.patch_switch.blockSignals(False)
        self.patch_update_btn.setVisible(shown and self._patch_applied and self._patch_outdated)
        self.patch_update_btn.setEnabled(enabled)
        self.patch_update_btn.setToolTip(update_tip)
        note = getattr(self, "_patch_note", "")
        self.patch_note.setText(note)
        self.patch_note.setVisible(shown and bool(note))

    def _on_patch_clicked(self, checked: bool) -> None:
        self.patch_toggled.emit(self._profile, bool(checked))

    def _on_patch_update_clicked(self) -> None:
        self.patch_update_requested.emit(self._profile)
