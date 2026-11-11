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
#: How long a notification call may block the GUI thread when the service hangs.
CALL_TIMEOUT_MS = 2_000


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


def still_running_text(titles: list[str]) -> tuple[str, str]:
    """``(summary, body)`` for a window closed while ``titles`` play (#114: no tray icon)."""
    names = sorted(titles)
    if len(names) > 1:
        who, verb, games = ", ".join(names[:-1]) + f" and {names[-1]}", "are", "games exit"
    else:
        who, verb, games = (names[0] if names else "Your game"), "is", "game exits"
    return (
        "Nadare is still running",
        f"{who} {verb} running. Nadare quits when the {games}. "
        "Start Nadare again to bring the window back.",
    )


def send(summary: str, body: str = "") -> bool:
    """Show a notification; ``True`` when the notification service accepted it."""
    try:
        from PyQt6.QtCore import QMetaType
        from PyQt6.QtDBus import QDBus, QDBusArgument, QDBusConnection, QDBusMessage

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        # A plain message with a short timeout: QDBusInterface introspects the service
        # synchronously, and a hung notification daemon froze the window for 25 s.
        message = QDBusMessage.createMethodCall(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
        )
        # Typed as the spec says (susssasa{sv}i): a plain 0, [] and {} went out as int32 and
        # untyped arrays, which strict services (KDE's, GNOME's) refuse with "no such method".
        message.setArguments(
            [
                "Nadare",
                QDBusArgument(0, QMetaType.Type.UInt.value),
                "nadare",
                summary,
                body,
                QDBusArgument([], QMetaType.Type.QStringList.value),
                # An empty {} is dropped from the message; one real hint keeps the a{sv}.
                # urgency is a byte (1 = normal); a QDBusVariant here went out as a variant
                # inside a variant (dbus-monitor), which GLib services cannot read.
                {"urgency": QDBusArgument(1, QMetaType.Type.UChar.value)},
                TIMEOUT_MS,
            ]
        )
        reply = bus.call(message, QDBus.CallMode.Block, CALL_TIMEOUT_MS)
        return (
            reply.type() == QDBusMessage.MessageType.ReplyMessage and reply.errorName() == ""
        )
    except Exception:  # noqa: BLE001 - a notification must never break an install
        log.debug("could not send a notification", exc_info=True)
        return False
