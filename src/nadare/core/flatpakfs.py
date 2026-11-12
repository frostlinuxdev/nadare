# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Which folders Nadare's Flatpak may use for games (#1211).

The Flatpak no longer gets the whole host (``--filesystem=host``): it gets the home folder,
``/mnt``, ``/media``, ``/run/media`` and a few named places. A folder outside them does not exist
inside the Flatpak, and the file picker hands it over as a portal path (``/run/user/<uid>/doc/…``),
which must never hold a game. ``/.flatpak-info`` lists what this instance may really use, including
what the user added with ``flatpak override``, so it is the source of truth here.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from . import sandbox

FLATPAK_INFO = Path("/.flatpak-info")
APP_ID = "io.github.frostlinuxdev.Nadare"

#: ``xdg-*`` names Flatpak accepts in ``filesystems=``, relative to the host home folder.
_XDG = {
    "xdg-data": ".local/share",
    "xdg-config": ".config",
    "xdg-cache": ".cache",
    "xdg-desktop": "Desktop",
    "xdg-documents": "Documents",
    "xdg-download": "Downloads",
    "xdg-music": "Music",
    "xdg-pictures": "Pictures",
    "xdg-videos": "Videos",
}


def _grants() -> list[str] | None:
    """The ``filesystems=`` entries, or ``None`` when they cannot be read (never block then)."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if not parser.read(FLATPAK_INFO, encoding="utf-8"):
            return None
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    raw = parser.get("Context", "filesystems", fallback="")
    return [entry.strip() for entry in raw.split(";") if entry.strip()]


def _granted_roots(grants: list[str], home: Path) -> tuple[bool, list[Path]]:
    """``(everything, writable roots)`` from the grants; read-only ones are left out."""
    roots: list[Path] = []
    for entry in grants:
        name, _, mode = entry.partition(":")
        if name.startswith("!") or mode == "ro":
            continue
        if name in ("host", "host-os"):  # host-os is /usr etc.; only "host" means everything
            if name == "host":
                return True, roots
            continue
        if name == "home":
            roots.append(home)
        elif name.startswith("~/"):
            roots.append(home / name[2:])
        elif name.startswith("/"):
            roots.append(Path(name))
        else:
            base, _, rest = name.partition("/")
            if base in _XDG:
                roots.append(home / _XDG[base] / rest if rest else home / _XDG[base])
    return False, roots


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def unreachable_reason(folder: str | os.PathLike[str]) -> str | None:
    """Why the Flatpak cannot keep a game in ``folder``; ``None`` when it can (or no Flatpak)."""
    if not sandbox.in_flatpak():
        return None
    path = Path(os.path.abspath(os.path.expanduser(str(folder))))
    grants = _grants()
    if grants is None:
        return None
    home = Path(os.path.expanduser("~"))
    portal = len(path.parts) > 4 and path.parts[1:3] == ("run", "user") and path.parts[4] == "doc"
    if not portal:
        app_id = os.environ.get("FLATPAK_ID") or APP_ID
        if _inside(path, home / ".var" / "app" / app_id):
            return None  # Nadare's own data folder is always there
        everything, roots = _granted_roots(grants, home)
        if everything or any(_inside(path, root) for root in roots):
            return None
    shown = path if not portal else "that folder"
    return (
        f"The Flatpak version of Nadare cannot use {shown}: it may only use ~/Games, /mnt, "
        "/media and /run/media (and folders you allowed), so your personal files stay out of "
        "its reach. Pick a folder there, or allow this one once in a terminal and restart "
        "Nadare:\n"
        f"flatpak override --user --filesystem={path if not portal else '<the real folder>'} "
        f"{APP_ID}"
    )


__all__ = ["APP_ID", "FLATPAK_INFO", "unreachable_reason"]
