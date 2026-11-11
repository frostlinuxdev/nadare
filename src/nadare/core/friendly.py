# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain words for the failures people hit most, with the raw text kept as details.

A failure dialog used to show ``str(exc)``: a URL and ``<urlopen error [Errno -3] ...>`` for
an offline PC, ``HTTP 403 for https://...`` for a refused download. ``user_text`` looks down
the exception's cause chain for a few well-known roots (offline, a server's HTTP status, a
full disk, a folder Nadare may not change, a damaged download) and leads with a sentence that
says what happened and what to do; the original text follows after ``DETAILS``. Anything it
does not recognise is returned exactly as the backend wrote it, since most backend messages
already say what to do.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import urllib.error

#: Separates the plain sentence from the raw text; ``split`` takes it apart again.
DETAILS = "\n\nDetails: "

_HTTP_IN_TEXT = re.compile(r"\bHTTP (\d{3}) for ")
_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_FORBIDDEN = {errno.EACCES, errno.EPERM, errno.EROFS}


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and what it was raised ``from``, outermost first (cycles are cut).

    Only explicit causes count: a backend that raises its own message inside an ``except``
    block (an implicit ``__context__``) chose those words, and they are kept.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not s for s in seen) and len(seen) < 16:
        seen.append(current)
        current = current.__cause__
    return seen


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    code = getattr(exc, "code", None)
    if isinstance(exc, urllib.error.HTTPError) and isinstance(code, int):
        return code
    match = _HTTP_IN_TEXT.search(str(exc))
    return int(match.group(1)) if match else None


def _http(status: int) -> str:
    if status in (401, 403):
        return (
            f"The download server refused the request (HTTP {status}). The publisher may "
            "block your region or have changed how the game is downloaded. Try again later; "
            "if it keeps happening, check for a Nadare update."
        )
    if status in (404, 410):
        return (
            f"The publisher's server no longer has this file (HTTP {status}). The game may "
            "have just been updated: press Retry in a few minutes, and check for a Nadare "
            "update if it keeps happening."
        )
    if status == 429:
        return (
            "The download server asks Nadare to slow down (HTTP 429). Wait a few minutes, "
            "then press Retry."
        )
    if status >= 500:
        return (
            f"The download server is having problems (HTTP {status}). This is on the "
            "publisher's side: wait a few minutes, then press Retry."
        )
    return ""


def _is_network(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(
        exc, (urllib.error.URLError, socket.gaierror, TimeoutError, ConnectionError)
    )


def _folder(filename: object) -> str:
    text = os.fsdecode(filename) if isinstance(filename, (str, bytes)) else ""
    return os.path.dirname(text) or text


def explain(exc: BaseException) -> str:
    """A plain sentence and the next step for ``exc``, or ``""`` when nothing better is known."""
    chain = _chain(exc)
    for cause in chain:
        status = _status(cause)
        if status is not None and status >= 400:
            text = _http(status)
            if text:
                return text
    for cause in chain:
        if _is_network(cause):
            return (
                "Nadare could not reach the download server. Check that this computer is "
                "online (and that a VPN or firewall is not blocking it), then press Retry."
            )
    for cause in chain:
        name = type(cause).__name__
        text = str(cause)
        if name == "IntegrityError" or (name == "SophonError" and "mismatch" in text):
            return (
                "A downloaded file arrived damaged (it did not match its checksum). Press "
                "Retry to download it again; if it keeps happening, try another network."
            )
    for cause in chain:
        if not isinstance(cause, OSError) or isinstance(cause, ConnectionError):
            continue
        where = _folder(cause.filename)
        if cause.errno in _FULL:
            place = f" ({where})" if where else ""
            return (
                f"The disk is full{place}. Free some space, or pick another drive in "
                "Settings, then press Retry."
            )
        if cause.errno in _FORBIDDEN:
            place = f" {where}" if where else " a folder it needs"
            return (
                f"Nadare is not allowed to change{place}. Check that the folder belongs "
                "to you and the drive is not read-only, then press Retry."
            )
    return ""


def user_text(exc: BaseException) -> str:
    """What a failure dialog shows for ``exc``: plain words first, the raw text as details."""
    raw = str(exc) or type(exc).__name__
    plain = explain(exc)
    return f"{plain}{DETAILS}{raw}" if plain else raw


def split(message: str) -> tuple[str, str]:
    """``(plain, details)`` of a ``user_text`` message; details are empty for a plain one."""
    head, sep, details = message.partition(DETAILS)
    return (head, details) if sep else (message, "")
