# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""FGO's SELinux policy module, removed only when it is really loaded (#1555).

The FGO setup installs a small policy module named ``lepton_binder`` on hosts
that enforce SELinux. Removing FGO should offer to take it away again, but only
when this build can run FGO at all and the module is present. Everything here is
read-only until :func:`remove` is called, and that only happens after the user
asks for it (the ``--remove-selinux-module`` flag or the GUI checkbox).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from . import process
from .i18n import translate

MODULE = "lepton_binder"
#: Plain-language name used in the CLI and GUI.
NOTE = "FGO's SELinux policy module (lepton_binder)"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def backend_available() -> bool:
    """Whether this build includes the FGO backend."""
    from . import backends

    return backends.is_available("fgo-lepton")


def module_loaded(run: Runner | None = None, which=shutil.which) -> bool:
    """Whether ``semodule -l`` lists :data:`MODULE`; a missing tool means no."""
    runner = run or subprocess.run
    if which("semodule") is None:
        return False
    try:
        result = runner(
            ["semodule", "-l"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout or ""
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == MODULE:
            return True
    return False


def available(run: Runner | None = None, which=shutil.which) -> bool:
    """True when FGO ships in this build and its SELinux module is loaded."""
    return backend_available() and module_loaded(run, which)


def removal_argv(profile: dict[str, Any] | None = None) -> list[str]:
    """The exact command that removes the module, through the password prompt.

    Always the system ``semodule``: lepton's ``setup-host-selinux.sh`` lives in the
    user's data folder, and running a file the user's own processes can change as
    root would hand root to anything running as the user. ``profile`` is accepted
    for callers that pass it and does not change the command.
    """
    del profile
    return ["pkexec", "semodule", "-r", MODULE]


def removal_command(profile: dict[str, Any] | None = None) -> str:
    """The removal command as one copy-paste line."""
    return shlex.join(removal_argv(profile))


def single_game_note(profile: dict[str, Any] | None) -> str:
    """The summary line for removing one FGO game: the command, never run."""
    if not profile or profile.get("backend") != "fgo-lepton" or not available():
        return ""
    return translate(
        "{module} was not changed. To remove it, run:\n{command}",
        module=NOTE, command=removal_command(profile),
    )


def remove(
    profile: dict[str, Any] | None = None,
    run: Runner | None = None,
    which=shutil.which,
) -> tuple[bool, str]:
    """Remove the module through ``pkexec``; never raise.

    The caller must have asked the user first. Returns ``(ok, message)`` where
    the message is plain enough to print or show in a dialog.
    """
    runner = run or subprocess.run
    if not available(run=runner, which=which):
        return False, translate("{module} is not loaded; nothing to remove.", module=NOTE)
    argv = removal_argv(profile)
    if which("pkexec") is None:
        fallback = "sudo " + shlex.join(argv[1:])
        return False, translate(
            "pkexec was not found. Run this as root: {command}", command=fallback,
        )
    try:
        result = runner(
            argv,
            env=process.child_environ(),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        fallback = "sudo " + shlex.join(argv[1:])
        return False, translate(
            "Could not ask for your password. Run this as root: {command}", command=fallback,
        )
    if result.returncode == 0:
        return True, translate("Removed {module}.", module=NOTE)
    if result.returncode == 126:
        return False, translate("Cancelled: no permission was given.")
    if result.returncode == 127:
        fallback = "sudo " + shlex.join(argv[1:])
        return False, translate(
            "The password was not accepted or the prompt could not be shown. "
            "Try again, or run this as root: {command}", command=fallback,
        )
    detail = ((result.stderr or result.stdout) or "").strip()[:300]
    return False, translate("Could not remove {module}.", module=NOTE) + (
        f" {detail}" if detail else ""
    )


__all__ = [
    "MODULE",
    "NOTE",
    "available",
    "backend_available",
    "module_loaded",
    "removal_argv",
    "removal_command",
    "remove",
    "single_game_note",
]
