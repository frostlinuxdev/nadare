# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Host variables games may inherit. Backend and user settings are added afterwards.

Profiles can opt in to individual host names with ``pass_env: ["http_proxy"]``;
``extra_env`` sets explicit values. Neither accepts patterns. The same keys work
inside a profile's ``extra`` mapping. Loader variables are deliberately absent:
only an explicit launch setting may supply them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .config import profile_value

log = logging.getLogger(__name__)

NAMES = frozenset(
    """
DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR XDG_SESSION_TYPE
XDG_CURRENT_DESKTOP DESKTOP_SESSION LANG LANGUAGE LOCPATH TZ HOME USER LOGNAME SHELL PATH
DBUS_SESSION_BUS_ADDRESS AUDIODEV ALSA_CONFIG_PATH ALSA_CONFIG_DIR ALSOFT_DRIVERS
OSS_AUDIODEV ALSA_CARD ALSA_PCM_CARD ALSA_PCM_DEVICE ALSA_PCM_SUBDEVICE
ALSA_MIXER_CARD ALSA_MIXER_DEVICE JACK_DEFAULT_SERVER JACK_SERVER JACK_NO_START_SERVER
XMODIFIERS GTK_IM_MODULE QT_IM_MODULE GDK_BACKEND QT_QPA_PLATFORM
DRI_PRIME mesa_glthread __GL_THREADED_OPTIMIZATIONS __GL_SYNC_TO_VBLANK
__GL_SHADER_DISK_CACHE __GL_SHADER_DISK_CACHE_PATH __GL_SHADER_DISK_CACHE_SKIP_CLEANUP
__GL_GSYNC_ALLOWED __GL_VRR_ALLOWED __GL_YIELD
SteamAppId SteamGameId GAMEID STORE ENABLE_VKBASALT FLATPAK_ID NADARE_FLATPAK
container WAYLANDDRV_PRIMARY_MONITOR NADARE_WINEBUS_SDL
ENABLE_GAMESCOPE_WSI EGL_PLATFORM OBS_VKCAPTURE GAMEMODERUNEXEC GDK_SCALE GDK_DPI_SCALE
QT_SCALE_FACTOR QT_AUTO_SCREEN_SCALE_FACTOR QT_SCREEN_SCALE_FACTORS QT_ENABLE_HIGHDPI_SCALING
TERM TMPDIR XDG_SESSION_DESKTOP XDG_SESSION_CLASS XDG_SESSION_ID XDG_SEAT XDG_VTNR SteamDeck SteamOS
""".split()
)
PREFIXES = (
    "XCURSOR_",
    "GAMESCOPE_",
    "LIBGL_",
    "GALLIUM_",
    "__EGL_",
    "GBM_",
    "ENABLE_VK_LAYER_",
    "DISABLE_VK_LAYER_",
    "DISABLE_LAYER_",
    "LC_",
    "XKB_DEFAULT_",
    "PULSE_",
    "PIPEWIRE_",
    "SDL_",
    "__GLX_",
    "__NV_",
    "__VK_",
    "VK_",
    "MESA_",
    "RADV_",
    "AMD_",
    "NVIDIA",
    "DXVK_",
    "VKD3D_",
    "PROTON_",
    "WINE",
    "STEAM_",
    "UMU_",
    "PRESSURE_VESSEL_",
    "MANGOHUD",
)


def pass_names(profile: Mapping[str, Any] | None) -> set[str]:
    """Only a list of exact host variable names opts in to inheritance."""
    value = profile_value(profile or {}, "pass_env")
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


def explicit_names(profile: Mapping[str, Any], extra: Mapping[str, str] | None = None) -> set[str]:
    """Names intentionally supplied by the user or the backend."""
    configured = profile_value(profile, "extra_env")
    return (
        pass_names(profile)
        | set(configured if isinstance(configured, dict) else ())
        | set(extra or ())
    )


def inherit(base: Mapping[str, str], profile: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Copy allowed host variables and log dropped names, never their values."""
    passed = pass_names(profile)
    env = {
        key: value
        for key, value in base.items()
        if key in NAMES
        or key in passed
        or key.startswith(PREFIXES)
        or (key.startswith("XDG_") and key.endswith(("_HOME", "_DIRS")))
    }
    dropped = tuple(sorted(base.keys() - env.keys()))
    log.debug(
        "Launch environment dropped names: %s",
        ", ".join(dropped),
        extra={"dropped_env_names": dropped},
    )
    return env
