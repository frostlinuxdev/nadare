# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Flatpak awareness: host-only tools and Proton without umu."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from . import paths

FLATPAK_INFO = Path("/.flatpak-info")

#: ``flatpak-spawn --env=K=V`` values land in the *host* process table, which
#: every local user can read; variables whose names look like credentials are
#: therefore refused rather than leaked.
_SECRET_ENV = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE"
    r"|API_?KEY|ACCESS_KEY|PRIVATE_KEY|SECRET_KEY|SESSION_KEY)(?:_|$)",
    re.IGNORECASE,
)


def _reject_secret_env(env: Mapping[str, str]) -> None:
    for key in env:
        if _SECRET_ENV.search(key):
            raise ValueError(
                f"refusing to pass {key!r} to the host: flatpak-spawn env values "
                "are exposed in the world-readable host process table"
            )


def in_flatpak() -> bool:
    """Return True when Nadare is running inside a Flatpak sandbox."""
    return os.environ.get("NADARE_FLATPAK") == "1" or FLATPAK_INFO.exists()


def host_argv(
    argv: list[str],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> list[str]:
    """Wrap ``argv`` so a host-only program runs outside the sandbox.

    Only the environment *delta* is forwarded, as ``--env=K=V`` arguments. Under
    Flatpak those cross the trust boundary into the host process table, so
    secret-like values are refused.
    """
    if not in_flatpak():
        return list(argv)
    delta = {k: v for k, v in (env or {}).items() if os.environ.get(k) != v}
    _reject_secret_env(delta)
    wrapped = ["flatpak-spawn", "--host"]
    if cwd:
        wrapped.append(f"--directory={cwd}")
    wrapped += [f"--env={k}={v}" for k, v in sorted(delta.items())]
    return wrapped + list(argv)


def host_which(name: str) -> str | None:
    """Locate a host program, asking ``flatpak-spawn`` when sandboxed."""
    if not in_flatpak():
        return shutil.which(name)
    try:
        result = subprocess.run(
            ["flatpak-spawn", "--host", "sh", "-c", 'command -v -- "$1"', "sh", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stripped = result.stdout.strip()
    if result.returncode == 0 and stripped:
        return stripped
    return None


def proton_direct(
    proton_dir: str | os.PathLike[str],
    prefix: str | os.PathLike[str],
    args: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Build a ``<runner>/proton run`` command for use inside a Flatpak.

    Proton expects the Wine prefix at ``<STEAM_COMPAT_DATA_PATH>/pfx``; a plain
    Wine prefix kept on disk is bridged with a ``pfx -> .`` symlink so the same
    directory keeps working. ``STEAM_COMPAT_CLIENT_INSTALL_PATH`` points at a
    private, empty directory rather than a game prefix or a real Steam install.
    """
    prefix = Path(prefix)
    pfx = prefix / "pfx"
    if not pfx.exists() and not pfx.is_symlink():
        try:
            pfx.symlink_to(".", target_is_directory=True)
        except OSError:
            pass
    argv = [str(Path(proton_dir) / "proton"), "run", *[str(a) for a in args]]
    client = paths.data_dir() / "proton-client"
    try:
        client.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env = {
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(client),
        "STEAM_COMPAT_DATA_PATH": str(prefix),
    }
    return argv, env


__all__ = [
    "FLATPAK_INFO",
    "host_argv",
    "host_which",
    "in_flatpak",
    "proton_direct",
]
