# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A desktop notification when a long operation ends.

Sent over the session bus to whatever shows notifications (``org.freedesktop.Notifications``:
KDE, GNOME, XFCE, Cinnamon and others) with Qt's own D-Bus module, so nothing extra has to be
installed. It never raises: no bus or no notification service simply means no notification.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Operations that end with a notification, and the words for them.
_PAST = {"install": "installed", "update": "updated", "repair": "repaired"}
_VERB = {"install": "installed", "update": "updated", "repair": "repaired"}
#: Seconds the notification stays up before the desktop may hide it.
TIMEOUT_MS = 10_000


def finished_text(operation: str, title: str, ok: bool) -> tuple[str, str] | None:
    """``(summary, body)`` for the end of ``operation`` on ``title``, or ``None`` for no notice."""
    if operation not in _PAST:
        return None
    if ok:
        return (
            f"{title} is {_PAST[operation]}",
            "Open Nadare to play." if operation != "repair" else "",
        )
    return f"{title} could not be {_VERB[operation]}", "Open Nadare for details."


def send(summary: str, body: str = "") -> bool:
    """Show a notification; ``True`` when the notification service accepted it."""
    try:
        from PyQt6.QtDBus import QDBusConnection, QDBusInterface

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        service = QDBusInterface(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            bus,
        )
        if not service.isValid():
            return False
        reply = service.call("Notify", "Nadare", 0, "nadare", summary, body, [], {}, TIMEOUT_MS)
        return reply.errorName() == ""
    except Exception:  # noqa: BLE001 - a notification must never break an install
        log.debug("could not send a notification", exc_info=True)
        return False
