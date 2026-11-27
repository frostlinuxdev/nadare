# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The close request a launch saved for Stop (#1421 ask 3).

Stop must not build a launch command while the player waits: resolving a runner,
migrating a runtime or generating locales can take far longer than the grace. A
launch therefore records what Stop needs beside the run lock (:mod:`nadare.core.runlock`),
from the command and environment it already resolved:

* ``taskkill``: one non-forced ``taskkill /IM <exe>`` per known game executable,
  run in the launch's own prefix and environment. With ``inside`` the command is the one
  the game runs inside its Sandbox, and Stop runs it there (:mod:`nadare.core.sandboxjoin`,
  #1766); without the Sandbox it runs as it is;
* ``x11``: the game is Sandboxed where Nadare cannot join it (the Flatpak), so Stop sends
  ``_NET_CLOSE_WINDOW`` to the game's windows instead of starting a Wine helper outside;
* ``none``: the launch knew no executable to ask (Stop goes straight to signals).

The file is private (0600) and holds only what Stop needs: the command, and the
launch environment *difference* — variables the launch added or changed, and never
one whose name looks like a credential (#1421 review). At Stop the difference is
laid over the process environment it runs in.
A missing or unreadable file is not an error; the game then stops the old way.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import atomicfile, runlock

#: Bumped when the shape changes; an older file is ignored, never guessed at.
#: 2: ``inside`` (#1766). An older Nadare would run an inside command outside the Sandbox.
VERSION = 2

#: The three things a saved close request can say.
KINDS = frozenset({"taskkill", "x11", "none"})

#: A variable whose name matches this is never written to the state file.
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|SESSION|AUTH|CREDENTIAL|KEY", re.IGNORECASE
)
#: Families whose variable names may contain a word above (``PROTON_KEY``) and are kept.
_KEPT_PREFIXES = ("WINE", "PROTON", "DXVK", "VKD3D", "STEAM_COMPAT", "UMU")


def _persisted_env(env: dict[str, str]) -> dict[str, str]:
    """The launch's own environment difference, minus credential-looking names."""
    return {
        name: value
        for name, value in env.items()
        if os.environ.get(name) != value
        and (
            name.upper().startswith(_KEPT_PREFIXES) or _SECRET_NAME.search(name) is None
        )
    }


def path(prefix: str | os.PathLike[str]) -> Path:
    """The saved request for ``prefix``, beside its run lock."""
    return runlock.state_path(prefix, ".stop.json")


def save(
    prefix: str | os.PathLike[str],
    *,
    kind: str,
    commands: list[list[str]] | tuple[tuple[str, ...], ...] = (),
    env: dict[str, str] | None = None,
    inside: bool = False,
) -> None:
    """Write the close request for ``prefix`` (private, atomic).

    ``env`` is the launch's environment; only its difference from this process's
    environment is kept, so the file does not carry the player's whole environment.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown close request kind: {kind!r}")
    payload = {
        "version": VERSION,
        "kind": kind,
        "inside": bool(inside),
        "commands": [[str(part) for part in argv] for argv in commands],
        "env": {
            str(key): str(value) for key, value in _persisted_env(env or {}).items()
        },
    }
    atomicfile.write(
        path(prefix), json.dumps(payload), mode=0o600, make_parent=True
    )


def load(prefix: str | os.PathLike[str]) -> dict | None:
    """The saved request for ``prefix``, or ``None`` when there is no usable one."""
    try:
        data = json.loads(path(prefix).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None
    kind = data.get("kind")
    commands = data.get("commands", [])
    env = data.get("env", {})
    inside = data.get("inside", False)
    if not isinstance(inside, bool):
        return None
    if kind not in KINDS or not isinstance(commands, list) or not isinstance(env, dict):
        return None
    clean: list[list[str]] = []
    for argv in commands:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            return None
        clean.append(list(argv))
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        return None
    return {
        "version": VERSION,
        "kind": kind,
        "inside": inside,
        "commands": clean,
        "env": dict(env),
    }


def clear(prefix: str | os.PathLike[str]) -> None:
    """Forget the saved request (after Stop, or when a launch replaces it)."""
    try:
        path(prefix).unlink()
    except OSError:
        pass
