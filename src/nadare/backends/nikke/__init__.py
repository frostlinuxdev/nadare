# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Launch NIKKE without ``nikke_launcher.exe`` (the Sail handshake).

``session.py`` turns the INTL service auth blob into the game-auth form the
Sail init carries; ``hosts.py`` holds the two pinned Windows helpers, the INTL
cache and the command lines; ``hosts_src/`` holds their C sources and
``build_hosts.sh`` builds them reproducibly. The backend wiring lives in
:mod:`nadare.backends.launcher` (``NikkeBackend``).
"""

from __future__ import annotations

from .hosts import (
    DEFAULT_RESOURCE_PATH,
    GAME_ID,
    INTL_HOST_NAME,
    PARENT_NAME,
    SAIL_HOST_NAME,
    STAGE_PARTS,
    GameFiles,
    HostError,
    cache_storage_dir,
    find_game,
    has_cache,
    host_env,
    intl_command,
    make_session_file,
    private_workdir,
    read_auth,
    restore_cache,
    sail_command,
    save_cache,
    stage_hosts,
    win_path,
)
from .session import build_session, trim_game_auth

__all__ = [
    "DEFAULT_RESOURCE_PATH",
    "GAME_ID",
    "INTL_HOST_NAME",
    "PARENT_NAME",
    "SAIL_HOST_NAME",
    "STAGE_PARTS",
    "GameFiles",
    "HostError",
    "build_session",
    "cache_storage_dir",
    "find_game",
    "has_cache",
    "host_env",
    "intl_command",
    "make_session_file",
    "private_workdir",
    "read_auth",
    "restore_cache",
    "sail_command",
    "save_cache",
    "stage_hosts",
    "trim_game_auth",
    "win_path",
]
