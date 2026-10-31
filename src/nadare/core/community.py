# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where the community lives: the Discord invite, read from the index so it can change.

The index is not signed, so a value from it is used only when it is a plain Discord invite
(https, Discord's own hosts, a single code). Anything else, or no answer, gives the built-in
invite. Nothing here opens a link: the caller does that when the user clicks.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from . import indexfetch

log = logging.getLogger(__name__)

#: Used whenever the index has no usable entry.
DISCORD_FALLBACK = "https://discord.gg/9WkaSgJBft"
#: The index document: ``{"discord": "https://discord.gg/<code>"}``.
LINKS_DOC = "community/links.json"

_CODE = re.compile(r"[A-Za-z0-9_-]{2,32}")


def is_discord_invite(url: Any) -> bool:
    """True only for ``https://discord.gg/<code>`` and ``https://discord.com/invite/<code>``."""
    if not isinstance(url, str) or url != url.strip() or not url.startswith("https://"):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.username or parts.password or port is not None or parts.query or parts.fragment:
        return False
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s != ""]
    if parts.path.endswith("/") and len(segments) > 0:
        return False
    if host == "discord.gg":
        return len(segments) == 1 and bool(_CODE.fullmatch(segments[0]))
    if host in ("discord.com", "www.discord.com"):
        return len(segments) == 2 and segments[0] == "invite" and bool(_CODE.fullmatch(segments[1]))
    return False


def _valid_doc(data: Any) -> bool:
    return isinstance(data, dict)


def discord_url() -> str:
    """The Discord invite to open: the index's when it is a real invite, else the built-in one.

    May touch the network (a cached index copy is used when offline), so call it off the GUI thread.
    """
    try:
        data = indexfetch.fetch_json(LINKS_DOC, validate=_valid_doc, max_bytes=4096).data
    except Exception as exc:  # noqa: BLE001 - the built-in invite always works
        log.debug("community links unavailable: %s", exc)
        return DISCORD_FALLBACK
    value = data.get("discord") if isinstance(data, dict) else None
    return value if is_discord_invite(value) else DISCORD_FALLBACK
