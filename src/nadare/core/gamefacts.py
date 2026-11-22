# SPDX-License-Identifier: GPL-3.0-or-later
"""The facts a game page shows, named once instead of derived from loosely related flags.

A saved Sandbox switch, the isolation of the process that is running, downloaded files and a
complete install are different facts. Treating them as one caused the Sandbox pill on a game that
runs unsandboxed, "nothing half-installed" after a partial update and a lost Resume entry (#850).
These small value types hold the rules; the GUI reads from them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .backends import Status


def update_available(status: Status | None) -> bool:
    facts = CommonStatus.from_status(status)
    return facts.state != "error" and facts.update_available and not facts.install.offers_resume()


def is_running(status: object | None) -> bool:
    """One running rule, including duck-typed worker results."""
    return CommonStatus.from_status(status).running


def primary_operation(status: Status | None) -> str | None:
    return None if status is None else CommonStatus.from_status(status).primary_operation()


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
    #: Why the switch cannot be changed although the Sandbox runs (a game that requires it).
    locked: str = ""

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
        return not self.blocked and not self.locked and not self.running

    def tooltip(self, default: str) -> str:
        if self.blocked:
            return self.blocked
        if self.locked:
            return self.locked
        if self.running:
            return "Stop the game to change this; it applies the next time you play."
        if self.chosen and not self.ready:
            return (
                "Sandbox cannot start here right now, so the game runs without it. Usually "
                "bubblewrap is not on this computer yet (it comes with the game runtime Nadare "
                "downloads on the first launch), or the game runtime was updated and Nadare asks "
                "at start-up whether to trust its new copy. `nadare doctor` says which."
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

    @classmethod
    def from_status(cls, status: Status | None) -> InstallFacts:
        """The install facts a backend status carries (``none`` when there is no status)."""
        if status is None:
            return cls(installed=False, incomplete=False)
        return CommonStatus.from_status(status).install

    def offers_resume(self) -> bool:
        return self.state() in ("paused", "interrupted")


def display_state(status: Status | None) -> str:
    """The state views show: an interrupted install is not an installed game."""
    if status is None:
        return ""
    facts = CommonStatus.from_status(status)
    if facts.state != "error" and facts.install.offers_resume():
        return "not_installed"
    return facts.state


StatusState = Literal["not_installed", "installed", "running", "error"]


@dataclass(frozen=True)
class CommonStatus:
    """Validated shared facts; backend-specific details remain opaque extensions.

    Malformed shared values fail closed to error. Unknown extension keys and their
    values are deliberately not interpreted. Status remains the backend wire type.
    """

    state: StatusState
    running: bool
    install: InstallFacts
    version: str | None = None
    message: str = ""
    update_available: bool = False
    login_required: bool = False
    logged_in: bool | None = None
    login_expired: bool = False
    can_install: bool = True
    can_login: bool = True
    can_update: bool = True
    can_repair: bool = False
    retired: bool = False
    latest_version: str = ""
    account: str = ""
    login_label: str = ""
    extensions: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_status(cls, status: object | None) -> CommonStatus:
        if isinstance(status, cls):
            return status
        if status is None:
            return cls("not_installed", False, InstallFacts(False, False))
        raw_state = getattr(status, "state", None)
        state = raw_state.strip().lower() if isinstance(raw_state, str) else ""
        invalid = state not in ("not_installed", "installed", "running", "error")
        raw_running = getattr(status, "running", False)
        invalid |= type(raw_running) is not bool
        running = raw_running is True or state == "running"
        raw_data = getattr(status, "data", {})
        if not isinstance(raw_data, Mapping):
            invalid = True
            raw_data = {}
        data: dict[str, object] = {}
        for key, value in raw_data.items():
            if isinstance(key, str):
                data[key] = value
            else:
                invalid = True

        def boolean(key: str, default: bool = False) -> bool:
            nonlocal invalid
            value = data.pop(key, default)
            if type(value) is not bool:
                invalid = True
                return default
            return value

        def text(key: str) -> str:
            nonlocal invalid
            value = data.pop(key, "")
            if not isinstance(value, str):
                invalid = True
                return ""
            return value

        version = getattr(status, "version", None)
        if version is not None and not isinstance(version, str):
            invalid = True
            version = None
        message = getattr(status, "message", "")
        if not isinstance(message, str):
            invalid = True
            message = ""
        logged_in = data.pop("logged_in", None)
        if logged_in is not None and type(logged_in) is not bool:
            invalid = True
            logged_in = None
        install = InstallFacts(
            state == "installed" or running, boolean("incomplete"), boolean("paused")
        )
        facts = cls(
            cast(StatusState, state),
            running,
            install,
            version,
            message,
            update_available=boolean("update_available"),
            login_required=boolean("login_required"),
            logged_in=logged_in,
            login_expired=boolean("login_expired"),
            can_install=boolean("can_install", True),
            can_login=boolean("can_login", True),
            can_update=boolean("can_update", True),
            can_repair=boolean("can_repair"),
            retired=boolean("retired"),
            latest_version=text("latest_version"),
            account=text("account"),
            login_label=text("login_label"),
            extensions=MappingProxyType(data),
        )
        if invalid:
            logging.getLogger(__name__).warning("Invalid backend status state or common fields")
            # The state is an error, but an explicit boolean running=True is still a fact: a
            # game may really be running and must stay stoppable. Anything else (``running=1``,
            # a string) is not a running signal, so the views agree on the error action.
            return replace(
                facts,
                state="error",
                running=raw_running is True,
                install=InstallFacts(False, False),
            )
        return facts

    def primary_operation(self) -> str:
        if self.running:
            return "stop"
        if self.state == "error":
            return "remove" if self.retired else "retry"
        if self.state == "not_installed":
            return "install" if self.can_install else "settings"
        if self.install.offers_resume():
            return "install"
        if self.update_available:
            return "update"
        if self.login_required and not self.logged_in:
            return "login"
        return "launch"
