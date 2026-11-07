# SPDX-License-Identifier: GPL-3.0-or-later
"""Common API keys and passwords never reach a game's environment (#861)."""

from __future__ import annotations

import pytest

from nadare.core import process


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_APIKEY",
        "DATABASE_PASSWORD",
        "DB_PASSWD",
        "AWS_SECRET_ACCESS_KEY",
        "STRIPE_SECRET_KEY",
        "MY_PRIVATE_KEY",
        "GCP_ACCESS_KEY",
        "SERVICE_CREDENTIALS",
        "openai_api_key",
        "GITHUB_TOKEN",
        "MY_API_TOKEN",
        "VAULT_SECRET",
    ],
)
def test_a_credential_looking_name_is_dropped(name):
    out = process.sanitize_game_env({name: "dummy", "KEEP": "1", "PATH": "/usr/bin"})
    assert name not in out and out["KEEP"] == "1"


@pytest.mark.parametrize(
    "name",
    [
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "HOME",
        "LANG",
        "XKB_DEFAULT_LAYOUT",
        "SDL_GAMECONTROLLERCONFIG",
        "DXVK_HUD",
        "WINEPREFIX",
        "PROTONPATH",
        "STEAM_COMPAT_DATA_PATH",
        "NVIDIA_PRIME_RENDER_OFFLOAD",
        "KEYBOARD_LAYOUT",
        "MONKEY",
        "PASSWORD_STORE_DIR",
    ],
)
def test_ordinary_variables_are_kept(name):
    out = process.sanitize_game_env({name: "x", "PATH": "/usr/bin"})
    assert out[name] == "x"


def test_the_whole_pipeline_drops_them_too():
    base = {"OPENAI_API_KEY": "dummy", "DATABASE_PASSWORD": "dummy", "DISPLAY": ":0"}
    out = process.sanitize_game_env(process.child_environ(base))
    assert "OPENAI_API_KEY" not in out and "DATABASE_PASSWORD" not in out
    assert out["DISPLAY"] == ":0"
