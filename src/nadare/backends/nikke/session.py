# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""NIKKE's Sail session: turn the INTL service auth blob into the game-auth form.

``intl_service.exe`` returns JSON in ``auth_encypt_data``. The game's Sail SDK
accepts the *game* auth blob the publisher launcher normally gets from
``AuthSvr/Login``, which is that JSON with top-level ``bind_list`` emptied:
passing the service blob unchanged makes the game reject the Sail init
(``kErrorInitializeReadInitializeInfoFailed``).

The module only transforms and fills the constants measured from a real
``cmd=100`` capture (2026-11-06). It never contains or mints credentials.
"""

from __future__ import annotations

import json
from typing import Any

GAME_ID = 29080
LAUNCHER_GAME_ID = 16601
ZONE_ID = 10001
CHANNEL_ID = 131
PLATFORM_LANGUAGE_CODE = 1033
PLATFORM_TYPE = 1
COMPLIANCE_REGION = "250"
BRANCH_ID = "1"
VERSION_ID = "0.0.0.74"
DISTRIBUTE_ID = "0"
#: Where the launcher put the game's resources (the game uses this as its patch root).
DEFAULT_GAME_RESOURCE_PATH = r"C:\NIKKE\Unity\com_proximabeta_NIKKE"

REQUIRED_AUTH_FIELDS = ("openid", "token", "pf", "pf_key")


def trim_game_auth(auth: dict[str, Any]) -> str:
    """Return the compact game-auth JSON the Sail init expects.

    ``bind_list`` is the only structural difference between the INTL service
    blob and the game-auth blob. Everything else (including nested
    ``extra_json``) is kept unchanged.
    """
    missing = [key for key in REQUIRED_AUTH_FIELDS if not auth.get(key)]
    if missing:
        raise ValueError(f"auth blob is missing required field(s): {', '.join(missing)}")

    game_auth = dict(auth)
    game_auth["bind_list"] = ""
    return _compact(game_auth)


def _channel(value: Any, default: int) -> int:
    """The sign-in channel from the service's reply; a missing or odd value is the default."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):  # inf from a 1e400 in the JSON (#1207 b036)
        return default


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def build_session(
    auth: dict[str, Any],
    *,
    game_resource_path: str = DEFAULT_GAME_RESOURCE_PATH,
    channel_id: int = CHANNEL_ID,
    adult_state: int = 1,
) -> dict[str, Any]:
    """Build the session document consumed by ``sail_host.exe``."""
    encrypted = trim_game_auth(auth)
    return {
        "game_id": GAME_ID,
        "launcher_game_id": LAUNCHER_GAME_ID,
        "channel_id": _channel(auth.get("channelid"), channel_id),
        "zone_id": ZONE_ID,
        "platform_language_code": PLATFORM_LANGUAGE_CODE,
        "platform_type": PLATFORM_TYPE,
        "open_id": auth["openid"],
        "token": auth["token"],
        "encypt_data": encrypted,
        "compliance_region": COMPLIANCE_REGION,
        "refresh_token": "",
        "pf": auth["pf"],
        "pf_key": auth["pf_key"],
        "branch_id": BRANCH_ID,
        "version_id": VERSION_ID,
        "distribute_id": DISTRIBUTE_ID,
        "game_resource_path": game_resource_path,
        "adult_state": adult_state,
        "is_intl_test_env": 0,
        "support_debug_mode": 0,
    }
