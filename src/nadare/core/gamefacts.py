# SPDX-License-Identifier: GPL-3.0-or-later
"""The facts a game page shows, named once instead of derived from loosely related flags.

A saved Sandbox switch, the isolation of the process that is running, downloaded files and a
complete install are different facts. Treating them as one caused the Sandbox pill on a game that
runs unsandboxed, "nothing half-installed" after a partial update and a lost Resume entry (#850).
These small value types hold the rules; the GUI reads from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends import Status


def primary_operation(status: Status | None) -> str | None:
    """The game page's main action for this status (``None`` when there is no clear one)."""
    if status is None:
        return None
    if status.running or status.state == "running":
        return "stop"
    if status.state == "error":
        # A retired kind (a launcher Nadare no longer runs) can only be removed (b23).
        return "remove" if (status.data or {}).get("retired") else "retry"
    if status.state == "not_installed":
        if (status.data or {}).get("can_install") is False:
            return "settings"
        return "install"
    data = status.data or {}
    if data.get("update_available"):
        return "update"
    if data.get("login_required") and not data.get("logged_in"):
        return "login"
    return "launch"


@dataclass(frozen=True)
class SandboxFacts:
    """The Sandbox switch against what the running process actually has."""

    supported: bool
    #: The saved switch. It applies at the next launch, not to a game already running.
    chosen: bool
    running: bool = False
    #: Whether the running process was started sandboxed; ``None`` when that is not known.
    active: bool | None = None
    #: Why the switch cannot be used on this machine ("" when it can).
    blocked: str = ""
    #: Whether a bubblewrap exists here; without one a game on by default plays unprotected.
    ready: bool = True

    def _can_run(self) -> bool:
        return self.ready and not self.blocked

    def protected_now(self) -> bool:
        """Whether the process now running (or about to start) is sandboxed."""
        if self.running and self.active is not None:
            return self.active
        return self.chosen and self._can_run()

    def pill(self) -> str:
        """The pill text for the game page ("" hides it)."""
        if not self.supported:
            return ""
        if self.protected_now():
            return "SANDBOX"
        if self.running and self.chosen and self._can_run():
            return "SANDBOX NEXT LAUNCH"
        return ""

    def editable(self) -> bool:
        return not self.blocked and not self.running

    def tooltip(self, default: str) -> str:
        if self.blocked:
            return self.blocked
        if self.running:
            return "Stop the game to change this; it applies the next time you play."
        if self.chosen and not self.ready:
            return (
                "Sandbox needs bubblewrap, which is not on this computer yet (it comes with the "
                "game runtime Nadare downloads on the first launch). Until then the game runs "
                "without it; `nadare doctor` says more."
            )
        return default


@dataclass(frozen=True)
class InstallFacts:
    """Installed, interrupted and paused are three different things."""

    installed: bool
    #: An install or update stopped part-way (files may be a mix of versions).
    incomplete: bool
    #: A download the user paused; its partial files are kept.
    paused: bool = False

    def state(self) -> str:
        """``paused``, ``interrupted``, ``complete`` or ``none``."""
        if self.paused:
            return "paused"
        if self.incomplete:
            return "interrupted"
        return "complete" if self.installed else "none"

    def offers_resume(self) -> bool:
        return self.state() in ("paused", "interrupted")
