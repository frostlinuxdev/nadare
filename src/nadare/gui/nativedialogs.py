# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Route file dialogs through the desktop portal (issue #652).

The Qt bundled with the AppImage ships only the GTK3 and portal platform themes,
so a theme named by the desktop (``kde``, ``lxqt``, ...) cannot be loaded and the
file dialog falls back to the bare Qt one. The portal theme asks the desktop's own
dialog over the session bus instead. The portal theme is used only when the
portal service is installed (always assumed inside Flatpak).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

__all__ = [
    "DEFAULT_DATA_DIRS",
    "PORTAL_THEME",
    "apply",
    "choose_platform_theme",
    "portal_installed",
    "qt_theme_available",
    "session_bus_available",
]

#: Plugin name of Qt's desktop-portal platform theme.
PORTAL_THEME = "xdgdesktopportal"

#: Where distributions install shared data when ``XDG_DATA_DIRS`` is unset.
DEFAULT_DATA_DIRS = ("/usr/local/share", "/usr/share")

_PORTAL_SERVICE = Path("dbus-1/services/org.freedesktop.portal.Desktop.service")

_OPT_OUT_VALUES = frozenset({"0", "false", "no", "off"})

_ASK_QT_LIBRARY = object()

log = logging.getLogger(__name__)


def session_bus_available(env: Mapping[str, str]) -> bool:
    """Whether a D-Bus session bus can be reached from this environment."""
    if env.get("DBUS_SESSION_BUS_ADDRESS", "").strip():
        return True
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        return False
    return (Path(runtime) / "bus").exists()


def portal_installed(
    env: Mapping[str, str], *, flatpak_info: Path = Path("/.flatpak-info")
) -> bool:
    """Whether the desktop portal D-Bus service is installed."""
    try:
        if flatpak_info.exists():
            return True
        configured = env.get("XDG_DATA_DIRS", "").strip()
        dirs = [part for part in configured.split(":") if part] or list(DEFAULT_DATA_DIRS)
        return any((Path(directory) / _PORTAL_SERVICE).exists() for directory in dirs)
    except OSError:
        return False


def _qt_library_plugin_dir() -> Path | None:
    try:
        from PyQt6.QtCore import QLibraryInfo

        return Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    except Exception:  # noqa: BLE001 - a broken Qt must not stop the GUI
        return None


def qt_theme_available(
    name: str, *, env: Mapping[str, str] | None = None, library_dir=_ASK_QT_LIBRARY
) -> bool:
    """Whether the Qt in use ships the platform theme plugin ``name``."""
    if env is None:
        env = os.environ
    plugin = Path("platformthemes") / f"libq{name}.so"
    try:
        plugin_paths = [Path(part) for part in env.get("QT_PLUGIN_PATH", "").split(":") if part]
        if library_dir is _ASK_QT_LIBRARY:
            library_dir = _qt_library_plugin_dir()
        if library_dir is not None:
            plugin_paths.append(Path(library_dir))
        return any((root / plugin).exists() for root in plugin_paths)
    except Exception:  # noqa: BLE001 - a broken Qt must not stop the GUI
        return False


def choose_platform_theme(
    env: Mapping[str, str],
    *,
    has_theme=None,
    session_bus: bool | None = None,
    portal_present: bool | None = None,
) -> str | None:
    """The platform theme to force, or ``None`` to leave the environment alone."""
    if env.get("NADARE_NATIVE_DIALOGS", "").strip().lower() in _OPT_OUT_VALUES:
        return None
    if has_theme is None:

        def has_theme(theme: str) -> bool:
            return qt_theme_available(theme, env=env)

    if session_bus is None:
        session_bus = session_bus_available(env)
    if portal_present is None:
        portal_present = portal_installed(env)
    if not session_bus or not portal_present or not has_theme(PORTAL_THEME):
        return None
    configured = env.get("QT_QPA_PLATFORMTHEME", "").strip()
    if not configured:
        return PORTAL_THEME
    names = [name.strip() for name in configured.split(":") if name.strip()]
    if any(name == PORTAL_THEME or has_theme(name) for name in names):
        return None
    return PORTAL_THEME


def apply(env: MutableMapping[str, str] | None = None) -> None:
    """Select the portal theme for this process when the environment allows it."""
    try:
        target = os.environ if env is None else env
        theme = choose_platform_theme(target)
        if theme is not None:
            target["QT_QPA_PLATFORMTHEME"] = theme
    except Exception:  # noqa: BLE001 - startup must survive a broken environment
        log.debug("could not select a platform theme", exc_info=True)
