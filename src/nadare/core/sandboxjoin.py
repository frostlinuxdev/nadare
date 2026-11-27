# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Run a helper inside a running game's Sandbox (#1766).

The Sandbox (bubblewrap) gives the game its own mount namespace with a private ``/tmp``,
where Wine's server socket lives, and by default its own PID namespace. A ``taskkill``
started outside would find no server and start a second one on the prefix. ``nsenter``
joins the Sandbox's namespaces instead; no root is needed, because the player's own
bubblewrap made them, so the player owns them.

The Sandbox is found from a game process: its outermost ancestor that is still in another
mount namespace than Nadare, started by bubblewrap itself. With its own PID namespace that
is bubblewrap's init; with the PID opt-out it is the command bwrap ran. Its namespaces are
the Sandbox's; umu's pressure-vessel container sits deeper and is not the one joined.

Bubblewrap is called ``bwrap`` on the host but ``srt-bwrap`` in umu's Steam Runtime, and
:func:`nadare.core.gamesandbox.locate` prefers the runtime's copy (the one Proton's container
uses and the one an AppArmor profile covers). Both names count as bubblewrap; checking for
the literal ``bwrap`` made a Sandbox that runs on that copy fall back to its windows (11-24).

Anything else (no such ancestor, one not started by bubblewrap, a process that is gone, no
``nsenter``) is ``None``, and Stop uses its other ways.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable

#: The namespaces joined, outermost first, with nsenter's flags for each.
_NAMESPACES = (
    ("user", ("-U", "--preserve-credentials")),
    ("mnt", ("-m",)),
    ("pid", ("-p",)),
)


def _proc(proc_root: str | os.PathLike[str], pid: int | str, *parts: str) -> str:
    return os.path.join(proc_root, str(pid), *parts)


def _ns(proc_root: str | os.PathLike[str], pid: int | str, name: str) -> str | None:
    try:
        return os.readlink(_proc(proc_root, pid, "ns", name))
    except OSError:
        return None


def _parent(proc_root: str | os.PathLike[str], pid: int) -> int | None:
    try:
        with open(_proc(proc_root, pid, "stat"), encoding="utf-8", errors="replace") as handle:
            stat = handle.read()
    except OSError:
        return None
    # The name may contain spaces and parentheses: the fields start after the last ")".
    fields = stat[stat.rfind(")") + 2 :].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _comm(proc_root: str | os.PathLike[str], pid: int) -> str:
    try:
        with open(_proc(proc_root, pid, "comm"), encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _is_bwrap(proc_root: str | os.PathLike[str], pid: int) -> bool:
    """Whether ``pid`` is bubblewrap's host-side monitor (``bwrap`` or ``srt-bwrap``)."""
    return _comm(proc_root, pid) in {"bwrap", "srt-bwrap"}


def sandbox_root(pid: int, *, proc_root: str | os.PathLike[str] = "/proc") -> int | None:
    """The host pid of the outermost process of the Sandbox ``pid`` runs in, or ``None``."""
    own = _ns(proc_root, "self", "mnt")
    if own is None or _ns(proc_root, pid, "mnt") in (None, own):
        return None
    root = pid
    for _ in range(64):  # a process tree is never this deep; a loop in /proc must not hang
        parent = _parent(proc_root, root)
        if not parent or _ns(proc_root, parent, "mnt") in (None, own):
            break
        root = parent
    else:
        return None
    parent = _parent(proc_root, root)
    return root if parent and _is_bwrap(proc_root, parent) else None


def enter_argv(
    pids: Iterable[int], *, proc_root: str | os.PathLike[str] = "/proc"
) -> list[str] | None:
    """``[nsenter, -t, <root>, ..., --]`` for the Sandbox of the first of ``pids``, or ``None``.

    Only namespaces that differ from Nadare's are joined: a game with the PID opt-out
    (``sandbox_share_pid``) shares Nadare's PID namespace, and joining it again would fail.
    """
    nsenter = shutil.which("nsenter")
    if nsenter is None:
        return None
    for pid in pids:
        root = sandbox_root(pid, proc_root=proc_root)
        if root is None:
            continue
        flags: list[str] = []
        for name, flag in _NAMESPACES:
            theirs = _ns(proc_root, root, name)
            if theirs is None:
                break
            if theirs != _ns(proc_root, "self", name):
                flags.extend(flag)
        else:
            return [nsenter, "-t", str(root), *flags, "--"]
    return None
