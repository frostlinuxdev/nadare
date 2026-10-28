# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit low #595 (game child environment): the game child environment is hardened."""

from __future__ import annotations

import logging

import pytest

from nadare.core import process


@pytest.fixture(params=[True, False], ids=["system-python", "no-system-python"])
def sanitize(request, monkeypatch):
    monkeypatch.setattr(process, "_system_python_exists", lambda: request.param)
    return process.sanitize_game_env


SECRET_NAMES = [
    "GITHUB_TOKEN",
    "MY_API_TOKEN",
    "CLIENT_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "GPG_AGENT_INFO",
    "GPG_TTY",
    "CZN_REFRESH",
]
KEPT_NAMES = [
    "HOME",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "LANG",
    "WINEPREFIX",
    "WINEDEBUG",
    "PROTON_ENABLE_WAYLAND",
    "PROTONPATH",
    "STEAM_COMPAT_DATA_PATH",
    "STEAM_COMPAT_CLIENT_INSTALL_PATH",
    "SDL_VIDEODRIVER",
    "TOKENIZER_CACHE",  # contains TOKEN but is not a *_TOKEN name
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_looking_variables_are_not_passed_to_the_game(sanitize, name):
    assert name not in sanitize({"PATH": "/usr/bin", name: "x"})


@pytest.mark.parametrize("name", KEPT_NAMES)
def test_ordinary_variables_are_kept(sanitize, name):
    assert sanitize({"PATH": "/usr/bin", name: "keep"})[name] == "keep"


def test_secret_names_are_matched_case_insensitively(sanitize):
    out = sanitize({"PATH": "/usr/bin", "github_token": "x", "Aws_Region": "y"})
    assert "github_token" not in out and "Aws_Region" not in out


def test_the_input_mapping_is_not_modified(sanitize):
    env = {"PATH": "/usr/bin", "GITHUB_TOKEN": "x"}
    sanitize(env)
    assert env == {"PATH": "/usr/bin", "GITHUB_TOKEN": "x"}


def test_relative_path_entries_are_removed(monkeypatch):
    monkeypatch.setattr(process, "_system_python_exists", lambda: True)
    out = process.sanitize_game_env({"PATH": ".:bin:/opt/tools:../x:/usr/bin"})
    entries = out["PATH"].split(":")
    assert all(entry.startswith("/") for entry in entries)
    assert "/opt/tools" in entries and "/usr/bin" in entries


def test_a_profile_prefix_cannot_be_overridden_by_extra_env(tmp_path):
    base = {"PATH": "/usr/bin"}
    declared = str(tmp_path / "declared")
    env = process.build_env(
        {"prefix": declared}, {"WINEPREFIX": str(tmp_path / "other")}, base=base
    )
    assert env["WINEPREFIX"] == declared


def test_a_profile_runner_cannot_be_overridden_by_extra_env(tmp_path):
    declared = str(tmp_path / "runner")
    env = process.build_env(
        {"runner": declared}, {"PROTONPATH": str(tmp_path / "evil")}, base={"PATH": "/usr/bin"}
    )
    assert env["PROTONPATH"] == declared


def test_the_steam_compat_path_cannot_be_redirected_by_extra_env(tmp_path):
    other = str(tmp_path / "other")
    env = process.build_env(
        {"prefix": str(tmp_path / "declared")},
        {"STEAM_COMPAT_DATA_PATH": other},
        base={"PATH": "/usr/bin"},
    )
    assert env.get("STEAM_COMPAT_DATA_PATH") != other


def test_overriding_a_reserved_variable_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        process.build_env(
            {"prefix": str(tmp_path / "p")},
            {"WINEPREFIX": str(tmp_path / "o")},
            base={"PATH": "/usr/bin"},
        )
    assert any("WINEPREFIX" in record.getMessage() for record in caplog.records)


def test_extra_env_still_sets_everything_else(tmp_path):
    env = process.build_env(
        {"prefix": str(tmp_path / "p")},
        {"DXVK_HUD": "fps", "WINEDLLOVERRIDES": "x=n"},
        base={"PATH": "/usr/bin"},
    )
    assert env["DXVK_HUD"] == "fps" and env["WINEDLLOVERRIDES"] == "x=n"


def test_without_a_declared_prefix_extra_env_may_set_it(tmp_path):
    env = process.build_env(None, {"WINEPREFIX": str(tmp_path / "p")}, base={"PATH": "/usr/bin"})
    assert env["WINEPREFIX"] == str(tmp_path / "p")


def test_the_same_value_in_extra_env_is_not_a_conflict(tmp_path, caplog):
    declared = str(tmp_path / "p")
    with caplog.at_level(logging.WARNING):
        env = process.build_env(
            {"prefix": declared}, {"WINEPREFIX": declared}, base={"PATH": "/usr/bin"}
        )
    assert env["WINEPREFIX"] == declared
    assert not [r for r in caplog.records if "WINEPREFIX" in r.getMessage()]
