# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""NVIDIA driver updated but the PC not restarted yet: say so before a game crashes.

After a driver update the new libraries are on disk while the old kernel module is still loaded.
Until a restart every Vulkan program fails to create its instance; a game under Proton crashes
inside DXVK with nothing that names the cause (flxpc, 2026-11-15: 615.78.08 loaded, 620.70.06
installed, Aether Gazer crashed three times).

The running version comes from ``/proc/driver/nvidia/version``; the installed one from the file
names of the 64-bit driver libraries. When none of them is the running version and they were
installed after the last boot (their ctime: packages keep their build time as mtime), Play is
refused with "restart your PC". Any other mismatch is only logged: a restart may not fix it.
Inside the Flatpak the game uses the Flatpak's own driver packages, which follow the running
module (:mod:`nadare.core.flatpakgl` checks those).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import sandbox
from .flatpakgl import parse_nvidia_version

log = logging.getLogger("nadare.nvidiarestart")

ROOT = Path("/")

#: The 64-bit library folders of the big distros.
LIBRARY_DIRS = (
    "usr/lib",
    "usr/lib64",
    "usr/lib/x86_64-linux-gnu",
    "usr/lib/x86_64-linux-gnu/nvidia/current",
)
_LIBRARY = re.compile(r"^lib(?:nvidia-glcore|GLX_nvidia)\.so\.(\d+\.\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class Mismatch:
    running: str
    installed: tuple[str, ...]
    #: The libraries were installed after the last boot: a restart loads the matching module.
    since_boot: bool


def _boot_time(root: Path) -> float | None:
    try:
        text = (root / "proc" / "stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _installed(root: Path) -> dict[str, float]:
    """Installed driver library versions -> the newest ctime of their files."""
    found: dict[str, float] = {}
    for rel in LIBRARY_DIRS:
        folder = root / rel
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            match = _LIBRARY.match(name)
            if match is None:
                continue
            try:
                info = os.lstat(folder / name)
            except OSError:
                continue
            version = match.group(1)
            found[version] = max(found.get(version, 0.0), info.st_ctime)
    return found


def check(root: Path) -> Mismatch | None:
    """The mismatch between the running NVIDIA module and its installed libraries, if any."""
    try:
        text = (root / "proc" / "driver" / "nvidia" / "version").read_text()
    except OSError:
        return None  # no NVIDIA driver loaded
    running = parse_nvidia_version(text)
    if running is None:
        return None
    installed = _installed(root)
    if not installed or running in installed:
        return None  # nothing found to compare (never guess), or the running one is there
    boot = _boot_time(root)
    since_boot = boot is not None and any(ctime > boot for ctime in installed.values())
    return Mismatch(running, tuple(sorted(installed)), since_boot)


def message(found: Mismatch) -> str:
    versions = ", ".join(found.installed)
    if found.since_boot:
        return (
            f"Your NVIDIA driver was updated to {versions}, but the old driver "
            f"({found.running}) is still running. Restart your PC, then press Play again: "
            "until then every game fails to start its graphics."
        )
    return (
        f"The running NVIDIA driver ({found.running}) does not match its installed libraries "
        f"({versions}). Games may fail to start their graphics until the driver is "
        "installed again."
    )


def guard_launch(backend) -> None:
    """Refuse Play while an updated NVIDIA driver waits for a restart.

    Never raises anything but that refusal: an unexpected error lets the launch go ahead.
    """
    from .backends import BackendError

    try:
        if sandbox.in_flatpak():
            return
        found = check(ROOT)
    except Exception:  # noqa: BLE001 - the check must never be what breaks a launch
        log.debug("NVIDIA driver check failed", exc_info=True)
        return
    if found is None:
        return
    if not found.since_boot:
        log.warning("%s", message(found))
        return
    raise BackendError(message(found))


__all__ = ["LIBRARY_DIRS", "ROOT", "Mismatch", "check", "guard_launch", "message"]
