# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Check that the host's graphics driver has matching Flatpak extensions.

Inside Nadare's Flatpak, NVIDIA's userspace comes from Flathub extensions named
after the exact host driver version. On a rolling distro the driver updates
first; until the matching extensions are installed, games fail with no
explanation. Mesa users need the 32-bit ``GL32.default`` extension instead.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import process, sandbox

ROOT = Path("/")

_VERSION = re.compile(r"Kernel Module.*?(\d+\.\d+(?:\.\d+)?)")


def parse_nvidia_version(text: str) -> str | None:
    """Return the host NVIDIA driver version from ``/proc/driver/nvidia/version``."""
    match = _VERSION.search(text)
    return match.group(1) if match else None


def extension_name(version: str) -> str:
    """The Flathub extension suffix for an NVIDIA driver version."""
    return "nvidia-" + version.replace(".", "-")


@dataclass(frozen=True)
class GlCheck:
    ok: bool
    summary: str
    fix: str = ""
    #: Flathub refs Nadare can install for the user, in the order they are needed.
    refs: tuple[str, ...] = ()


def _cannot_tell() -> GlCheck:
    return GlCheck(
        True,
        "Could not read the host's graphics driver, so the Flatpak graphics packages "
        "were not checked.",
    )


def _graphics_result(summary: str, refs: tuple[str, ...]) -> GlCheck:
    fix = "flatpak install flathub " + " ".join(refs) if refs else ""
    return GlCheck(False, summary, fix, refs)


def _nvidia_check(version: str, gl64: Path, gl32: Path) -> GlCheck:
    name = extension_name(version)
    has_64 = (gl64 / name).is_dir()
    has_32 = (gl32 / name).is_dir()
    if has_64 and has_32:
        return GlCheck(True, f"NVIDIA {version}, 64-bit and 32-bit packages present")

    base = "org.freedesktop.Platform."
    refs = []
    if not has_64:
        refs.append(f"{base}GL.{name}")
    if not has_32:
        refs.append(f"{base}GL32.{name}")
    # Most Nadare users are not terminal people (the user, 2026-11-15): say what to press,
    # never a command, and never "use the AppImage" (not a fix). A brand-new driver only
    # waits for Flathub, which adds it soon; Flatpak's own update fetches it too.
    fix_words = (
        " Press Install, or install the updates in your software center. If neither finds "
        "it yet, Flathub has not added this driver yet; it usually does soon."
    )
    if not has_64 and not has_32:
        summary = (
            f"Your NVIDIA driver was updated to {version}, and the Flatpak does not have its "
            "matching graphics package yet, so games will not start." + fix_words
        )
    elif not has_32:
        summary = (
            f"Your NVIDIA driver is {version}, but the Flatpak's 32-bit graphics package "
            "for it is missing, so some games will not start." + fix_words
        )
    else:
        summary = (
            f"Your NVIDIA driver is {version}, but the Flatpak's 64-bit graphics package "
            "for it is missing, so games will not start." + fix_words
        )
    return _graphics_result(summary, tuple(refs))


def check(root: Path) -> GlCheck:
    """Check the Flatpak's graphics extensions without ever raising."""
    gl64 = root / "usr" / "lib" / "x86_64-linux-gnu" / "GL"
    gl32 = root / "app" / "lib" / "i386-linux-gnu" / "GL"
    version_file = root / "proc" / "driver" / "nvidia" / "version"

    try:
        if not root.is_dir():
            return _cannot_tell()
        try:
            text = version_file.read_text()
        except FileNotFoundError:
            text = ""  # no NVIDIA driver: Mesa
        except OSError:
            return _cannot_tell()
    except OSError:
        return _cannot_tell()

    version = parse_nvidia_version(text)
    if version is not None:
        result = _nvidia_check(version, gl64, gl32)
    elif not (gl32 / "default").is_dir():
        result = _graphics_result(
            "The Flatpak's 32-bit graphics package is missing, so some games will not "
            "start.",
            ("org.freedesktop.Platform.GL32.default//25.08",),
        )
    else:
        result = GlCheck(True, "Mesa, 32-bit package present")

    # Flatpak's i386 Compat extension is what provides the 32-bit system libraries;
    # without it even the 32-bit GL extension has nothing to load.
    compat_ref = "org.freedesktop.Platform.Compat.i386//25.08"
    if (root / "app" / "lib" / "i386-linux-gnu" / "libc.so.6").exists():
        return result
    compat_summary = (
        "The Flatpak's 32-bit system libraries are missing, so 32-bit games will not "
        "start."
    )
    if result.ok:
        return _graphics_result(compat_summary, (compat_ref,))
    return _graphics_result(
        compat_summary + " " + result.summary, (compat_ref, *result.refs)
    )


def _install_failure(stderr: str) -> str:
    """A failure message with flatpak's own last line, plus the new-driver fallback."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    detail = lines[-1] if lines else "no reason was given"
    return (
        f"The packages could not be installed: {detail}. If your driver is brand new, "
        "Flathub may not have added it yet; it usually does soon, so try again later or "
        "install the updates in your software center."
    )


def install_missing(
    refs: tuple[str, ...], *, run=subprocess.run, timeout: int = 1800
) -> tuple[bool, str]:
    """Install missing Flathub refs per user, through the host's Flatpak.

    Called from inside Nadare's Flatpak; ``flatpak-spawn --host`` runs the host
    command. A per-user install needs no password and is still found by a
    system-installed Nadare. Never raises: the caller shows the message.
    """
    if not refs:
        return True, ""

    env = process.child_environ()
    adds = [
        "flatpak-spawn",
        "--host",
        "flatpak",
        "remote-add",
        "--user",
        "--if-not-exists",
        "flathub",
        "https://dl.flathub.org/repo/flathub.flatpakrepo",
    ]
    installs = [
        "flatpak-spawn",
        "--host",
        "flatpak",
        "install",
        "--user",
        "-y",
        "--noninteractive",
        "flathub",
        *refs,
    ]
    try:
        added = run(adds, env=env, capture_output=True, text=True, timeout=timeout)
        if added.returncode != 0:
            return False, _install_failure(added.stderr)
        installed = run(installs, env=env, capture_output=True, text=True, timeout=timeout)
        if installed.returncode != 0:
            return False, _install_failure(installed.stderr)
    except subprocess.TimeoutExpired:
        return False, "Installing the packages took too long and was stopped."
    except FileNotFoundError:
        return False, "The host's flatpak command was not found, so nothing was installed."
    except OSError as exc:
        return False, f"The packages could not be installed: {exc}"
    return True, "Installed. Restart Nadare to use the new graphics packages."


def problem() -> GlCheck | None:
    """Return the graphics problem inside the Flatpak, otherwise None."""
    try:
        if not sandbox.in_flatpak():
            return None
        result = check(ROOT)
        return None if result.ok else result
    except Exception:  # noqa: BLE001 - a startup check must never break the app
        return None


#: Wait (up to 30 s) until the old Nadare has let go of its single-instance socket, then start
#: the GUI: a start while the old one still runs would only hand over to it and exit.
_RESTART_SCRIPT = (
    'n=0; while [ -S "$1" ] && [ "$n" -lt 150 ]; do sleep 0.2; n=$((n+1)); done; '
    "exec nadare gui"
)


def restart_argv(socket: str) -> list[str]:
    """The command that starts a fresh Nadare once this one has quit.

    Flatpak mounts its graphics packages when an instance starts, so a child inside this
    sandbox would still miss the new ones. The portal (``flatpak-spawn`` without ``--host``)
    starts a new instance of the app instead; ``--latest-version`` picks up an app update too.
    """
    return [
        "flatpak-spawn",
        "--latest-version",
        "/bin/sh",
        "-c",
        _RESTART_SCRIPT,
        "nadare-restart",
        socket,
    ]


def restart_nadare(socket: str, *, popen=subprocess.Popen) -> bool:
    """Start the fresh Nadare in the background; the caller quits right after. Never raises."""
    try:
        popen(  # noqa: S603 - fixed argv built above
            restart_argv(socket),
            env=process.child_environ(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    return True


def guard_launch(backend) -> None:
    """Refuse to start a game while the Flatpak lacks the driver's graphics package.

    Without it the game fails or runs without the GPU, and nothing says why. A Steam game
    is started by the host's Steam (#1224), so the Flatpak's packages do not matter there.
    Never raises anything but the refusal: an unexpected error lets the launch go ahead.
    """
    from .backends import BackendError

    try:
        if getattr(backend, "kind", "") == "steam":
            return
        found = problem()
    except Exception:  # noqa: BLE001 - the check must never be what breaks a launch
        return
    if found is None or found.ok:
        return
    raise BackendError(
        found.summary + " (The Install button is on the banner at the top of Nadare; "
        "restart Nadare after it finishes.)"
    )


__all__ = [
    "ROOT",
    "GlCheck",
    "check",
    "extension_name",
    "guard_launch",
    "install_missing",
    "parse_nvidia_version",
    "problem",
    "restart_argv",
    "restart_nadare",
]
