# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Detect and explain Ubuntu's AppArmor restriction on user namespaces.

Ubuntu 24.04 sets ``kernel.apparmor_restrict_unprivileged_userns=1``. The
``bwrap`` binary shipped with the Steam Runtime then fails with ``setting up
uid map: Permission denied``, so prefix creation (and the WebView2 install)
cannot start. An AppArmor profile that allows ``userns`` for only that binary
fixes it without weakening the host for every other program.

The checks here are deliberately conservative and never raise: anything
uncertain means "not restricted" and nothing is blocked.
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import subprocess

from . import sandbox

#: The sysctl Ubuntu 24.04+ uses to gate unprivileged user namespaces.
SYSCTL_PATH = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"

#: Name of the AppArmor profile Nadare installs.
PROFILE_NAME = "nadare-srt-bwrap"

#: Where the profile is written on the host.
PROFILE_PATH = f"/etc/apparmor.d/{PROFILE_NAME}"

#: First sentence of :func:`hint`, used to recognise and split an embedded hint.
HINT_HEAD = (
    "Ubuntu's AppArmor blocks the user namespace the game's runtime sandbox "
    "needs (kernel.apparmor_restrict_unprivileged_userns=1), so the game's "
    "Windows environment cannot start."
)

#: What the friendly dialog says before the shell details.
SHORT_MESSAGE = (
    "Ubuntu is blocking the game's sandbox. Nadare needs a one-time permission to continue."
)

#: Output fragments that mean the runtime sandbox could not create a namespace.
_FAILURE_LINES = (
    "setting up uid map",
    "no permissions to create new namespace",
    "creating new namespace failed",
)


def restricted(path: str = SYSCTL_PATH) -> bool:
    """Return True only when the AppArmor userns sysctl is explicitly ``1``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            value = fh.read().strip()
    except (OSError, ValueError):
        return False
    return value == "1"


def _srt_bwrap_paths() -> list[str]:
    """Return the Steam Runtime bwrap copies already downloaded, newest first."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    pattern = os.path.join(
        glob.escape(data_home),
        "umu",
        "*",
        "pressure-vessel",
        "libexec",
        "steam-runtime-tools-0",
        "srt-bwrap",
    )
    try:
        return sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    except OSError:
        return []


def probe(run=subprocess.run) -> bool | None:  # noqa: ANN001
    """Try the bundled ``srt-bwrap`` for real: True works, False blocked, None unknown.

    The sysctl alone can give a false alarm (a profile or a distro tweak may
    already allow the binary), so ``nadare doctor`` asks the sandbox itself.
    ``None`` means the runtime is not downloaded yet or the result is unclear.
    Never raises.
    """
    for path in _srt_bwrap_paths()[:1]:
        try:
            result = run(
                [path, "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            return True
        output = (getattr(result, "stderr", "") or "") + (getattr(result, "stdout", "") or "")
        if any(is_failure_line(line) for line in output.splitlines()):
            return False
        return None
    return None


def profile_installed(path: str = PROFILE_PATH) -> bool:
    """Return True when Nadare's AppArmor profile file exists."""
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def is_failure_line(line: str) -> bool:
    """Return True for bwrap output that indicates a blocked user namespace."""
    if not line:
        return False
    lowered = line.lower()
    return any(fragment in lowered for fragment in _FAILURE_LINES)


def profile_text() -> str:
    """Return the AppArmor profile, scoped to the bundled ``srt-bwrap``."""
    return (
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        f"profile {PROFILE_NAME} "
        "@{HOME}/.local/share/umu/*/pressure-vessel/libexec/"
        "steam-runtime-tools-0/srt-bwrap flags=(unconfined) {\n"
        "  userns,\n"
        "}\n"
    )


def install_command() -> str:
    """Return the shell snippet that writes and loads the AppArmor profile."""
    return "\n".join(
        [
            f"sudo tee {PROFILE_PATH} >/dev/null <<'EOF'",
            profile_text().strip(),
            "EOF",
            f"sudo apparmor_parser -r {PROFILE_PATH}",
        ]
    )


def hint() -> str:
    """Return an actionable explanation of the AppArmor user-namespace block."""
    return (
        HINT_HEAD
        + "\n"
        "One command fixes it:\n"
        + install_command()
        + "\n"
        "Alternatively, sudo sysctl -w "
        "kernel.apparmor_restrict_unprivileged_userns=0 also works, but it is "
        "less safe because it turns the restriction off for every program."
    )


def with_hint(message: str, *, blocked: bool) -> str:
    """Append :func:`hint` to ``message`` only when a namespace block was seen."""
    if not blocked:
        return message
    return message + "\n" + hint()


def has_hint(message: str) -> bool:
    """Return True when ``message`` carries the AppArmor explanation."""
    return HINT_HEAD in message


def split_hint(message: str) -> tuple[str, str]:
    """Split ``message`` into the text before the hint and the hint itself."""
    index = message.find(HINT_HEAD)
    if index < 0:
        return message, ""
    return message[:index].strip(), message[index:]


def fix_script(path: str = PROFILE_PATH, parser: str = "apparmor_parser") -> str:
    """Return one shell command that writes and loads the profile.

    It is built only from constants, so no user input ever reaches the shell.
    """
    return (
        f"printf '%s' {shlex.quote(profile_text())} > {shlex.quote(path)}"
        f" && {shlex.quote(parser)} -r {shlex.quote(path)}"
    )


def can_fix() -> bool:
    """Return True when Nadare can offer the one-click password prompt."""
    return shutil.which("pkexec") is not None and not sandbox.in_flatpak()


def install_profile(run=subprocess.run) -> tuple[bool, str]:
    """Install the profile through ``pkexec``; never raise."""
    try:
        result = run(
            ["pkexec", "/bin/sh", "-c", fix_script()],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "Could not start the password prompt. Use Copy fix command instead."
    code = result.returncode
    if code == 0:
        return True, "Done. Press Install again."
    if code == 126:
        return False, "Cancelled: no permission was given."
    if code == 127:
        return (
            False,
            "The password was not accepted or the prompt could not be shown. "
            "Try again, or copy the command instead.",
        )
    detail = (result.stderr or result.stdout).strip()[:300]
    return False, "The fix failed: " + detail


__all__ = [
    "HINT_HEAD",
    "PROFILE_NAME",
    "PROFILE_PATH",
    "SHORT_MESSAGE",
    "SYSCTL_PATH",
    "can_fix",
    "fix_script",
    "has_hint",
    "hint",
    "install_command",
    "install_profile",
    "is_failure_line",
    "profile_installed",
    "probe",
    "profile_text",
    "restricted",
    "split_hint",
    "with_hint",
]
