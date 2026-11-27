# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-game icons for menu and Steam shortcuts (#1876 item 4).

The game's own art (its icon, else its cover, else its best image) is cut to a centred square
and saved as a 256x256 PNG. Natively it goes in the user's hicolor theme. A Flatpak cannot
write there, so it uses its own data folder, which the host sees at the same path; shortcuts
name the file by its full path, so both work without an icon-theme refresh.

Qt is imported only here, and only when an icon is made, so :mod:`shortcuts` stays pure Python.
Every function is best-effort: a missing or broken image means the shortcut keeps Nadare's icon.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import artwork, atomicfile, sandbox
from .logging import get_logger

log = get_logger(__name__)

#: Edge of the square icon, in pixels.
SIZE = 256


def icon_dir() -> Path:
    """Where shortcut icons go: the hicolor theme natively, Nadare's data folder in a Flatpak."""
    base = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"))
    if sandbox.in_flatpak():
        return base / "nadare" / "shortcut-icons"
    return base / "icons" / "hicolor" / f"{SIZE}x{SIZE}" / "apps"


def icon_file(profile: str) -> Path:
    """The icon file for ``profile``, whether or not it exists yet."""
    from .shortcuts import slug

    return icon_dir() / f"nadare-{slug(profile)}.png"


def existing(profile: str) -> Path | None:
    """The icon file for ``profile`` if it was made, else ``None``."""
    path = icon_file(profile)
    return path if path.is_file() else None


def source_image(data: Mapping[str, Any]) -> Path | None:
    """The art an icon is made from: the icon, else the cover, else the best image on disk."""
    profile = dict(data)
    for kind in ("icon", "cover"):
        found = artwork.resolve_kind(profile, kind)
        if found is not None:
            return found
    return artwork.resolve(profile)


def _render(source: Path) -> bytes | None:
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
        from PyQt6.QtGui import QImage
    except Exception:  # noqa: BLE001 - no Qt: the shortcut keeps Nadare's icon
        return None
    try:
        with artwork._qt_images():
            image = QImage(str(source))
            if image.isNull():
                return None
            side = min(image.width(), image.height())
            square = image.copy(
                (image.width() - side) // 2, (image.height() - side) // 2, side, side
            )
            scaled = square.scaled(
                SIZE,
                SIZE,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            raw = QByteArray()
            buffer = QBuffer(raw)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not scaled.save(buffer, "PNG"):
                return None
            buffer.close()
            return bytes(raw.data())
    except Exception as exc:  # noqa: BLE001 - Qt closing for exit, or an odd image
        log.debug("no shortcut icon from %s: %s", source, exc)
        return None


def write(profile: str, data: Mapping[str, Any]) -> Path | None:
    """Make or update ``profile``'s icon from its art; ``None`` when there is none to use."""
    source = source_image(data)
    if source is None:
        return None
    png = _render(source)
    if png is None:
        return None
    path = icon_file(profile)
    try:
        if path.is_file() and path.read_bytes() == png:
            return path
        atomicfile.write(path, png, mode=0o644, make_parent=True)
    except OSError as exc:
        log.warning("could not save the shortcut icon %s: %s", path, exc)
        return None
    return path


def remove(profile: str) -> bool:
    """Delete ``profile``'s icon; ``True`` when one was there."""
    try:
        icon_file(profile).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove the shortcut icon for %s: %s", profile, exc)
        return False
    return True
