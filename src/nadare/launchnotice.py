# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Say why a game did not start when ``nadare launch`` ran from a shortcut (#1876).

A menu, desktop or Steam shortcut runs ``nadare launch -p <game>`` with ``Terminal=false``, so
a game that is not installed, needs an update or is on hold did nothing at all when clicked:
the reason went to a stderr nobody reads. With no terminal it now also shows a notification.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import Any, Protocol

from .core import i18n

log = logging.getLogger(__name__)


class _HasProfiles(Protocol):
    profiles: Mapping[str, Mapping[str, Any]]


def has_terminal() -> bool:
    """Whether someone can read stderr: false for a menu, desktop or Steam shortcut."""
    try:
        return bool(sys.stderr is not None and sys.stderr.isatty())
    except (OSError, ValueError):
        return False


def refused(config: _HasProfiles, name: str, message: str) -> None:
    """Notify that ``name`` did not start and why, unless a terminal shows the error already."""
    if has_terminal():
        return
    data = config.profiles.get(name) if name else None
    title = str((data or {}).get("title") or name or "The game")
    try:
        from .gui import notify

        notify.send(
            i18n.translate("{title} did not start", title=title),
            f"{message}\n" + i18n.translate("Open Nadare for details."),
        )
    except Exception:  # noqa: BLE001 - a missing Qt or bus must never change the exit code
        log.debug("could not send the launch notification", exc_info=True)


def refused_launch(
    command: str, config: _HasProfiles | None, name: str | None, message: str | None
) -> None:
    """:func:`refused` for ``nadare launch`` only; no ``message`` or another command: nothing.

    ``config`` is None when the settings could not be read: the game is named by its key.
    """
    if command == "launch" and message:
        refused(config or _NoProfiles(), name or "", message)


def notice_not_accepted(command: str, config: _HasProfiles, name: str | None, notice: str) -> None:
    """Print the account notice and how to accept it; a shortcut, with no terminal, notifies."""
    print(notice)
    print(
        i18n.translate("rerun with --accept-account-notice to accept the account notice"),
        file=sys.stderr,
    )
    message = i18n.translate("Open Nadare and accept the account notice first.")
    refused_launch(command, config, name, message)


class _NoProfiles:
    profiles: Mapping[str, Mapping[str, Any]] = {}
