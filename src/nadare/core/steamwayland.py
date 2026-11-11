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


#: ``LaunchUndo.written`` of an undo saved before Nadare recorded what it wrote.
UNKNOWN = object()


@dataclass(frozen=True)
class LaunchUndo:
    """What an undo puts back: the earlier launch options, or ``None`` for "there were none".

    ``written`` is what Nadare set; the undo only applies while Steam still has exactly that,
    so options the player typed in Steam afterwards are never overwritten. ``UNKNOWN`` marks
    an undo saved before this was recorded.
    """

    previous: str | None
    written: str | None | object = UNKNOWN


def _split(options: str) -> tuple[str, str]:
    """``(what comes before %command%, %command% and the arguments)``.

    Only the part before ``%command%`` is the launcher's environment and wrappers; what follows
    is the game's own arguments, which must never be read or rewritten as a setting (#897).
    Options without ``%command%`` are all arguments (Steam appends them to the game)."""
    index = options.find("%command%")
    if index < 0:
        return "", options
    return options[:index], options[index:]


def has_wayland(options: str | None) -> bool:
    """Whether these launch options already switch native Wayland on.

    The shell keeps the last assignment of a repeated variable, so that one decides (#970)."""
    head, _tail = _split(options or "")
    found = [match.group(2) for match in _ASSIGNMENT.finditer(head)]
    return bool(found) and found[-1] == "1"


def _sets(head: str, pair: str) -> bool:
    return re.search(rf"(^|\s){re.escape(pair.split('=', 1)[0])}=", head) is not None


def has_variables(options: str | None, pairs: tuple[str, ...]) -> bool:
    """Whether every variable in ``pairs`` is set before ``%command%``, with any value.

    A value the player chose counts: only a missing variable is something to add (#970)."""
    head, _tail = _split(options or "")
    return all(_sets(head, pair) for pair in pairs)


_PAIR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_.:/+-]*$")


def with_wayland(options: str | None, extra: tuple[str, ...] = ()) -> str:
    """The launch options with native Wayland on, keeping everything the player wrote.

    ``extra`` are ``NAME=value`` pairs the game needs together with it (``UMU_ID=0``): each is
    added in front unless the player already set that variable before ``%command%``, whose value
    then stays. Text after ``%command%`` is never touched.
    """
    for pair in extra:
        if not _PAIR.fullmatch(pair):
            raise ValueError(f"not a plain NAME=value launch variable: {pair!r}")
    text = (options or "").strip()
    if "%command%" not in text:
        text = f"{VARIABLE}=1 %command%" + (f" {text}" if text else "")
    else:
        head, tail = _split(text)
        found = list(_ASSIGNMENT.finditer(head))
        if found:
            # The shell keeps the last of a repeated variable: leave one, set to 1 (#970).
            for match in reversed(found[1:]):
                head = head[: match.start()] + head[match.end() :]
            head = _ASSIGNMENT.sub(rf"\g<1>{VARIABLE}=1", head, count=1)
        else:
            head = f"{VARIABLE}=1 {head}"
        text = head + tail
    head, tail = _split(text)
    missing = [pair for pair in extra if not _sets(head, pair)]
    if missing:
        head = head.replace(f"{VARIABLE}=1", f"{VARIABLE}=1 " + " ".join(missing), 1)
        text = head + tail
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
