# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Helpers and constants shared by the parts of nadare.backends.czn.backend.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import atexit
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("nadare.backends.czn.backend")

UPDATE_MODULE = "nadare.backends.czn.update"

#: Written into a prefix Nadare created, so uninstall knows it may delete it.
PREFIX_MARKER = ".nadare-prefix"

#: STOVE's own page where an account agrees to the game's terms (the ``406401`` answer links it).
TERMS_URL = (
    "https://policy.onstove.com/?inflow_path=LAUNCHER&show_play_button=Y"
    "&game_id=STOVE_CHAOSZERO&lang=en"
)

#: ``czn_auth.py`` exit codes -> a user-safe message. Its own table: the auth
#: worker's 2 does not mean the updater's 2, so reusing ``EXIT_MESSAGES`` told
#: the user "cannot reach the update server" for an expired QR / rejected signin.
AUTH_EXIT_MESSAGES: dict[int, str] = {
    2: "STOVE login returned an unexpected error; try again in a minute "
    "(if it keeps happening, open the logs)",
    3: "STOVE rejected or expired the sign-in. Scan a fresh QR code and try again.",
    4: "Could not reach STOVE. Check your internet connection and try again.",
    5: "This STOVE account has not accepted Chaos Zero Nightmare's terms of service yet. "
    "Read and accept them in Nadare when it asks, or open " + TERMS_URL + " in your browser "
    "while signed in to the same STOVE account, then try again.",
}


def _remember_notice() -> str:
    """The notice when the saved login cannot be stored on this device."""
    return (
        "Remember me unavailable: this system cannot store your login securely. "
        "Nadare will ask you to sign in again next time."
    )

#: Process-wide live sessions, keyed by profile name. The GUI builds a fresh
#: backend object for every operation (``gui/workers.py``), so a QR login must
#: outlive the object that performed it; a separate process cannot share this
#: and must rely on ``remember_me`` + the keyring.
_LIVE_LOCK = threading.Lock()

_LIVE_SESSIONS: dict[str, dict[str, str]] = {}

#: Keyring reads are cached once per process so ``status()`` does not unlock the
#: user's keyring on every poll.
_KEYRING_CACHE: dict[str, dict[str, str] | None] = {}


def _live_get(profile: str) -> dict[str, str] | None:
    with _LIVE_LOCK:
        cred = _LIVE_SESSIONS.get(profile)
        return dict(cred) if cred else None


def _live_set(profile: str, refresh_token: str, member_no: str) -> None:
    with _LIVE_LOCK:
        _LIVE_SESSIONS[profile] = {"refresh_token": refresh_token, "member_no": member_no}


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _profile_setting(profile: dict[str, Any], key: str) -> Any:
    """Read a profile key from the top level or its ``extra`` mapping."""
    value = profile.get(key)
    if value not in (None, ""):
        return value
    extra = profile.get("extra")
    if isinstance(extra, dict):
        return extra.get(key)
    return None

#: Session paths already handed to ``atexit`` this process (one hook each).
_registered_ephemeral: set[Path] = set()


def _register_ephemeral(path: Path) -> None:
    """Ensure ``session.json`` is removed on process exit as a backstop.

    The launch path deletes it in a ``finally``; this covers the case where the
    whole launcher is closed while that thread is still unwinding (or blocked on
    a detached child's inherited stdio). Each path is registered once per
    process, so repeated launches do not pile up exit hooks.
    """
    target = Path(path)
    if target in _registered_ephemeral:
        return
    _registered_ephemeral.add(target)
    atexit.register(_unlink_quietly, target)
