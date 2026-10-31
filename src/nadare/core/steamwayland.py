# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Native Wayland for a Steam game: the launch option, and whether the runner can honour it.

Valve's own Proton ships only the X11 driver of Wine (``winex11.so``), so a game on it always
runs through XWayland, which flickers in some windows. DW-Proton also ships ``winewayland.so`` and
switches to it when the game's launch option sets ``PROTON_ENABLE_WAYLAND=1``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VARIABLE = "PROTON_ENABLE_WAYLAND"
_ASSIGNMENT = re.compile(rf"(^|\s){VARIABLE}=(\S*)")


@dataclass(frozen=True)
class LaunchUndo:
    """What an undo puts back: the earlier launch options, or ``None`` for "there were none"."""

    previous: str | None


def has_wayland(options: str | None) -> bool:
    """Whether these launch options already switch native Wayland on."""
    return any(match.group(2) == "1" for match in _ASSIGNMENT.finditer(options or ""))


_PAIR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_.:/+-]*$")


def with_wayland(options: str | None, extra: tuple[str, ...] = ()) -> str:
    """The launch options with native Wayland on, keeping everything the player wrote.

    ``extra`` are ``NAME=value`` pairs the game needs together with it (``UMU_ID=0``): each is
    added in front unless the player already set that variable, whose value then stays.
    """
    for pair in extra:
        if not _PAIR.fullmatch(pair):
            raise ValueError(f"not a plain NAME=value launch variable: {pair!r}")
    text = (options or "").strip()
    if _ASSIGNMENT.search(text):
        text = _ASSIGNMENT.sub(rf"\g<1>{VARIABLE}=1", text, count=1)
    elif "%command%" in text:
        text = f"{VARIABLE}=1 {text}"
    else:
        text = f"{VARIABLE}=1 %command%" + (f" {text}" if text else "")
    missing = [
        pair for pair in extra if not re.search(rf"(^|\s){re.escape(pair.split('=', 1)[0])}=", text)
    ]
    if missing:
        text = text.replace(f"{VARIABLE}=1", f"{VARIABLE}=1 " + " ".join(missing), 1)
    return text


def runner_has_wayland(proton: Path | None) -> bool:
    """Whether the Proton whose ``proton`` script is ``proton`` ships Wine's Wayland driver."""
    if proton is None:
        return False
    return (Path(proton).parent / "files/lib/wine/x86_64-unix/winewayland.so").is_file()


def wayland_session(env: Mapping[str, str] | None = None) -> bool:
    """Whether the desktop session is Wayland (native Wayland means nothing under X11)."""
    env = os.environ if env is None else env
    return env.get("XDG_SESSION_TYPE") == "wayland" or bool(env.get("WAYLAND_DISPLAY"))
