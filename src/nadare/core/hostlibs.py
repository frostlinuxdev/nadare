# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Host 32-bit library preflight for running a Proton build directly.

When ``umu-run`` is not on ``PATH`` the CZN backend runs a Steam-Runtime
("slr") Proton build directly, so Proton loads the *host's* 32-bit libraries.
On a host without them (no ``libfreetype6:i386`` etc.) ``wineboot`` prints
"Wine cannot find the FreeType font library" and the WebView2 installer then
hangs forever with no message. This module detects the common case *before*
Proton starts and produces an actionable hint instead.

The check is deliberately conservative: anything uncertain (``ldconfig`` not
runnable, or output we do not understand) returns ``None`` and never blocks.
"""

from __future__ import annotations

import subprocess

from . import process
from .shortcuts import command_hint

#: 32-bit sonames Proton/Wine need early during prefix creation.
REQUIRED_32BIT = (
    "libfreetype.so.6",
    "libudev.so.1",
    "libXext.so.6",
    "libxkbcommon.so.0",
)

#: Hints for the common package names, kept short.
_DISTRO_HINT = (
    "e.g. libfreetype6:i386 on Debian/Ubuntu, freetype.i686 on Fedora, "
    "lib32-freetype2 on Arch"
)


def parse_ldconfig(text: str) -> tuple[set[str], set[str]]:
    """Split ``ldconfig -p`` output into (32-bit, 64-bit) soname sets.

    Cache lines look like::

        \\tlibfreetype.so.6 (libc6) => /usr/lib/i386-linux-gnu/libfreetype.so.6
        \\tlibfreetype.so.6 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libfreetype.so.6

    A tag containing ``x86-64`` is 64-bit; a tag containing ``libc6`` without
    ``x86-64`` is 32-bit; anything else (e.g. ``libc6,AArch64``) is ignored.
    """
    sonames_32bit: set[str] = set()
    sonames_64bit: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or "=>" not in line:
            continue
        name = line.split(" ", 1)[0]
        tag = ""
        if "(" in line and ")" in line:
            tag = line[line.index("(") + 1 : line.index(")")]
        if "x86-64" in tag:
            sonames_64bit.add(name)
        elif tag.strip() == "libc6":
            sonames_32bit.add(name)
    return sonames_32bit, sonames_64bit


def _ldconfig_sonames(run) -> tuple[set[str], set[str]] | None:  # noqa: ANN001
    """Return (32-bit, 64-bit) sonames from the first ldconfig that answers."""
    for candidate in ("/usr/sbin/ldconfig", "/sbin/ldconfig", "/usr/bin/ldconfig", "ldconfig"):
        try:
            result = run(
                [candidate, "-p"],
                env=process.child_environ(),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = getattr(result, "stdout", "") or ""
        if not text.strip():
            continue
        sonames = parse_ldconfig(text)
        if sonames[0] or sonames[1]:
            return sonames
    return None


def missing_gl(run=subprocess.run) -> list[str] | None:  # noqa: ANN001
    """Return the 64-bit GL/EGL sonames absent from this host, or ``None`` if unknown.

    Only matters when Proton runs directly (no ``umu-run``): without libglvnd
    even native Wayland and DXVK cannot open a window (#132). Never raises.
    """
    sonames = _ldconfig_sonames(run)
    if sonames is None:
        return None
    return [name for name in REQUIRED_GL if name not in sonames[1]]


def gl_install_command(missing: list[str], family: str | None) -> str | None:
    """A copy-paste command installing the GL packages for ``missing``."""
    table = _GL_PACKAGES.get(family or "")
    if not table:
        return None
    packages: list[str] = []
    for soname in missing:
        package = table.get(soname)
        if package and package not in packages:
            packages.append(package)
    if not packages:
        return None
    return _INSTALL[family or ""].replace(
        "sudo dpkg --add-architecture i386 && sudo apt update && ", ""
    ).format(pkgs=" ".join(packages))


def missing_32bit(run=subprocess.run) -> list[str] | None:  # noqa: ANN001
    """Return the required 32-bit sonames absent from this host, in order.

    Runs ``ldconfig -p`` from the system locations first (``/usr/sbin``, ``/sbin``,
    ``/usr/bin``); ``PATH`` is only a last resort, so a look-alike earlier on
    ``PATH`` is not run when the real one exists. Returns ``None`` when ``ldconfig``
    cannot be run or its output has neither 32- nor 64-bit entries — i.e.
    "unknown", never a block.
    Never raises.
    """
    for candidate in ("/usr/sbin/ldconfig", "/sbin/ldconfig", "/usr/bin/ldconfig", "ldconfig"):
        try:
            result = run(
                [candidate, "-p"],
                env=process.child_environ(),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = getattr(result, "stdout", "") or ""
        if not text.strip():
            continue
        sonames_32bit, sonames_64bit = parse_ldconfig(text)
        if not sonames_32bit and not sonames_64bit:
            continue
        return [name for name in REQUIRED_32BIT if name not in sonames_32bit]
    return None


#: 64-bit GL/EGL sonames a directly-run Proton needs from the host (#132).
REQUIRED_GL = ("libGL.so.1", "libEGL.so.1")

#: GL soname -> package, per distro family (glvnd dispatch libraries).
_GL_PACKAGES: dict[str, dict[str, str]] = {
    "debian": {"libGL.so.1": "libgl1", "libEGL.so.1": "libegl1"},
    "fedora": {"libGL.so.1": "libglvnd-glx", "libEGL.so.1": "libglvnd-egl"},
    "arch": {"libGL.so.1": "libglvnd", "libEGL.so.1": "libglvnd"},
    "suse": {"libGL.so.1": "libGL1", "libEGL.so.1": "libEGL1"},
}

#: soname -> package, per distro family.
_PACKAGES: dict[str, dict[str, str]] = {
    "debian": {
        "libfreetype.so.6": "libfreetype6:i386",
        "libudev.so.1": "libudev1:i386",
        "libXext.so.6": "libxext6:i386",
        "libxkbcommon.so.0": "libxkbcommon0:i386",
    },
    "fedora": {
        "libfreetype.so.6": "freetype.i686",
        "libudev.so.1": "systemd-libs.i686",
        "libXext.so.6": "libXext.i686",
        "libxkbcommon.so.0": "libxkbcommon.i686",
    },
    "arch": {
        "libfreetype.so.6": "lib32-freetype2",
        "libudev.so.1": "lib32-systemd",
        "libXext.so.6": "lib32-libxext",
        "libxkbcommon.so.0": "lib32-libxkbcommon",
    },
    "suse": {
        "libfreetype.so.6": "libfreetype6-32bit",
        "libudev.so.1": "libudev1-32bit",
        "libXext.so.6": "libXext6-32bit",
        "libxkbcommon.so.0": "libxkbcommon0-32bit",
    },
}

_FAMILY_IDS = (
    ("debian", {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "neon"}),
    ("fedora", {"fedora", "rhel", "centos", "nobara", "rocky", "almalinux"}),
    ("arch", {"arch", "cachyos", "manjaro", "endeavouros", "garuda", "artix"}),
    ("suse", {"opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap", "sles"}),
)

_INSTALL = {
    "debian": "sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install {pkgs}",
    "fedora": "sudo dnf install {pkgs}",
    "arch": "sudo pacman -S --needed {pkgs}",
    "suse": "sudo zypper install {pkgs}",
}

_FAMILY_NOTE = {
    "fedora": "If dnf reports a version conflict, run `sudo dnf upgrade` first.",
    "arch": "This needs the [multilib] repository enabled in /etc/pacman.conf.",
}


def _os_release_ids(text: str) -> set[str]:
    ids: set[str] = set()
    for raw in text.splitlines():
        key, _, val = raw.strip().partition("=")
        if key not in ("ID", "ID_LIKE"):
            continue
        ids.update(val.strip().strip("\"'").lower().split())
    return ids


def distro_family(os_release: str | None = None) -> str | None:
    """``debian``/``fedora``/``arch``/``suse`` from /etc/os-release, else ``None``."""
    if os_release is None:
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                os_release = fh.read()
        except OSError:
            return None
    ids = _os_release_ids(os_release)
    for family, members in _FAMILY_IDS:
        if ids & members:
            return family
    return None


def install_command(missing: list[str], family: str | None) -> str | None:
    """A copy-paste command installing ``missing``, or ``None`` if unknown."""
    table = _PACKAGES.get(family or "")
    if not table:
        return None
    packages: list[str] = []
    for soname in missing:
        package = table.get(soname)
        if package and package not in packages:
            packages.append(package)
    if not packages:
        return None
    return _INSTALL[family or ""].format(pkgs=" ".join(packages))


def hint(missing: list[str], os_release: str | None = None) -> str:
    """Text telling the user how to fix the missing libraries.

    On a recognised distro this contains the exact command to copy and paste;
    otherwise it falls back to example package names.
    """
    head = f"Missing 32-bit libraries: {', '.join(missing)}."
    why = (
        "Nadare normally downloads its own runtime, which needs none of them, but "
        "that was not possible here (offline, a blocked download, or Ubuntu's "
        f"AppArmor restriction; `{command_hint('doctor')}` shows which). Without it, "
        "the game's Windows environment needs these libraries on your system."
    )
    family = distro_family(os_release)
    command = install_command(missing, family)
    if command:
        note = _FAMILY_NOTE.get(family or "", "")
        return (
            f"{head}\n{why}\nInstall them with:\n    {command}\n"
            + (f"{note}\n" if note else "")
            + "Or install umu-launcher (umu-run), which brings its own runtime."
        )
    return (
        f"{head}\n{why}\nInstall umu-launcher (umu-run) or your distro's 32-bit "
        f"libraries ({_DISTRO_HINT})."
    )


__all__ = [
    "REQUIRED_32BIT",
    "REQUIRED_GL",
    "distro_family",
    "gl_install_command",
    "missing_gl",
    "hint",
    "install_command",
    "missing_32bit",
    "parse_ldconfig",
]
