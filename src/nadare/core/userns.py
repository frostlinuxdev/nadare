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

from . import process, sandbox

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

#: Said under :data:`SHORT_MESSAGE`: Close is not "play without it" (the user kept the launch
#: strict, #1338), so say what Close means.
WITHOUT_IT = (
    "The game cannot start until this is allowed. You can allow it now, or the next time you "
    "press Play."
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
    """Return the Steam Runtime bwrap copies already downloaded, newest first.

    Search Nadare's owned runtime and both shared locations used by older/system umu.
    """
    from . import umu

    roots = umu.runtime_roots()
    found: list[str] = []
    for root in roots:
        pattern = os.path.join(
            glob.escape(root),
            "*",
            "pressure-vessel",
            "libexec",
            "steam-runtime-tools-0",
            "srt-bwrap",
        )
        found += [path for path in glob.glob(pattern) if path not in found]
    try:
        return sorted(found, key=os.path.getmtime, reverse=True)
    except OSError:
        return []


def probe(run=subprocess.run) -> bool | None:  # noqa: ANN001
    """Try the bundled ``srt-bwrap`` for real: True works, False blocked, None unknown.

    The sysctl alone can give a false alarm (a profile or a distro tweak may
    already allow the binary), so ``nadare doctor`` asks the sandbox itself.
    ``None`` means the runtime is not downloaded yet or the result is unclear. Only a copy the
    Sandbox recorded and that still matches is run: the folder is writable by a game, and this
    runs on the host (#965). Never raises.
    """
    from . import gamesandbox  # late: gamesandbox imports this module

    copies = permitted_runtime_copies(gamesandbox.recorded_runtime_copies())
    for path in copies[:1]:
        try:
            result = run(
                [path, "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
                env=process.child_environ(),
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


def permitted_runtime_copies(copies: list[str]) -> list[str]:
    """Do not run a new-location bwrap under the old Ubuntu profile."""
    from pathlib import Path

    from . import umu

    if runtime_profile_outdated():
        base = umu.runtime_base().resolve()
        return [copy for copy in copies if not Path(copy).resolve().is_relative_to(base)]
    return copies


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
    from . import umu

    legacy = "@{HOME}/.local/share/umu/*/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
    home_shared = umu.shared_runtime_roots()[0]
    attachments = [legacy, *[_runtime_attachment(root) for root in umu.runtime_roots()
                             if root != home_shared]]
    profiles = []
    for index, attachment in enumerate(dict.fromkeys(attachments)):
        name = PROFILE_NAME if index == 0 else f"{PROFILE_NAME}-{index}"
        profiles.append(f"profile {name} {attachment} flags=(unconfined) {{\n  userns,\n}}\n")
    return "abi <abi/4.0>,\ninclude <tunables/global>\n" + "".join(profiles)


def _runtime_attachment(root) -> str:  # noqa: ANN001
    # Quote whitespace and escape AppArmor glob syntax in the literal data path.
    literal = "".join("\\" + c if c in '\\"*?[]{}^' else c for c in str(root))
    return '\"' + literal + '/*/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap\"'


RUNTIME_MOVED_MESSAGE = (
    "Nadare moved its runtime. One more admin prompt is needed. "
    "Until then, games keep using the shared runtime folder."
)


def runtime_profile_outdated() -> bool:
    """Keep the old location until the installed Ubuntu profile permits the new one."""
    from . import umu

    if not restricted() or not profile_installed(PROFILE_PATH):
        return False
    try:
        with open(PROFILE_PATH, encoding="utf-8") as profile:
            return _runtime_attachment(umu.runtime_base() / "umu") not in profile.read()
    except FileNotFoundError:
        return False  # first install uses the existing initial-permission flow
    except OSError:
        return True  # cannot establish permission: retain the working shared location


def install_command() -> str:
    """Return the shell snippet that writes and loads the AppArmor profile."""
    return "sudo /bin/sh -c " + shlex.quote(fix_script())


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

    Paths and profile contents are shell quoted. Publish only after the parser loads it:
    a failed upgrade must leave the old profile detectable.
    """
    return (
        f"tmp=$(mktemp {shlex.quote(path + '.XXXXXX')}) || exit 1; "
        "trap 'rm -f -- \"$tmp\"' EXIT; "
        f"printf '%s' {shlex.quote(profile_text())} > \"$tmp\""
        f" && {shlex.quote(parser)} -r \"$tmp\""
        ' && chmod 644 "$tmp"'
        f" && mv -- \"$tmp\" {shlex.quote(path)}"
    )


def can_fix() -> bool:
    """Return True when Nadare can offer the one-click password prompt."""
    return shutil.which("pkexec") is not None and not sandbox.in_flatpak()


def install_profile(run=subprocess.run) -> tuple[bool, str]:
    """Install the profile through ``pkexec``; never raise."""
    try:
        result = run(
            ["pkexec", "/bin/sh", "-c", fix_script()],
            env=process.child_environ(),
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
        return True, "Done."
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


#: The launch message when Ubuntu blocks the Steam Runtime: the GUI opens its fix dialog for
#: any message carrying :func:`hint` (#1338: asked before the launch, not after a failure).
BLOCKED_BEFORE_LAUNCH = (
    "Ubuntu blocks the Steam Runtime this game runs in until a one-time permission is given."
)


def blocks_runtime() -> bool:
    """Whether the Steam Runtime's bwrap would be refused here (Ubuntu 24.04+).

    Without Nadare's profile, "unknown" (the runtime is not downloaded yet) and a probe that
    crashes count as blocked: umu's first run would fail the same way, so the player gets the
    one-time fix first. With the profile file present, only a probe that really fails counts
    (the file can be left behind by a fix that did not load). Never raises.
    """
    try:
        if not restricted():
            return False
        installed = profile_installed()
    except Exception:  # noqa: BLE001 - cannot tell: do not block a launch on it
        return False
    try:
        result = probe()
    except Exception:  # noqa: BLE001 - a crashing probe is "unknown"
        result = None
    if installed:
        return result is False
    return result is not True
