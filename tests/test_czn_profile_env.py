# SPDX-License-Identifier: GPL-3.0-or-later
"""CZN reads the profile's Environment setting like every other Wine-based backend."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core.backends import BackendError


@pytest.fixture
def make_backend_context(tmp_path):
    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


@pytest.fixture(autouse=True)
def _no_runner_downloads(monkeypatch):
    def offline(self, rid, latest):
        raise BackendError("offline (no runner downloads in tests)")

    monkeypatch.setattr(CznBackend, "_download_runner", offline)


def _backend(make_backend_context, tmp_path, monkeypatch, extra_env=None):
    proton = tmp_path / "MyProton"
    proton.mkdir(exist_ok=True)
    (proton / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    profile = {"prefix": str(tmp_path / "prefix"), "runner": str(proton)}
    if extra_env is not None:
        profile["extra_env"] = extra_env
    return CznBackend(make_backend_context("stove-czn", profile))


def test_settings_offers_an_environment_field_for_czn():
    option = next((o for o in CznBackend.options if o.key == "extra_env"), None)
    assert option is not None
    assert option.kind == "env" and option.label == "Environment"
    assert "WAYLANDDRV_PRIMARY_MONITOR" in option.help  # the case people ask for


def test_the_profiles_environment_reaches_the_game(make_backend_context, tmp_path, monkeypatch):
    backend = _backend(
        make_backend_context, tmp_path, monkeypatch, {"WAYLANDDRV_PRIMARY_MONITOR": "DP-1"}
    )
    _argv, env = backend._prefix_command(["game.exe"])
    assert env["WAYLANDDRV_PRIMARY_MONITOR"] == "DP-1"


def test_no_environment_setting_changes_nothing(make_backend_context, tmp_path, monkeypatch):
    backend = _backend(make_backend_context, tmp_path, monkeypatch)
    _argv, env = backend._prefix_command(["game.exe"])
    assert "WAYLANDDRV_PRIMARY_MONITOR" not in env


@pytest.mark.parametrize("bad", ["DP-1", ["A=1"], 5, None, ""])
def test_a_malformed_environment_setting_is_ignored_not_a_crash(
    make_backend_context, tmp_path, monkeypatch, bad
):
    backend = _backend(make_backend_context, tmp_path, monkeypatch, bad)
    _argv, env = backend._prefix_command(["game.exe"])
    assert env["WINEPREFIX"] == str(backend.prefix())


def test_the_launch_pins_win_over_the_profile(make_backend_context, tmp_path, monkeypatch):
    other = str(tmp_path / "elsewhere")
    backend = _backend(
        make_backend_context, tmp_path, monkeypatch,
        {"WINEPREFIX": other, "PROTONPATH": other, "STEAM_COMPAT_DATA_PATH": other},
    )
    _argv, env = backend._prefix_command(["game.exe"])
    assert env["WINEPREFIX"] == str(backend.prefix())
    assert env["PROTONPATH"] == str(tmp_path / "MyProton")
    assert env.get("STEAM_COMPAT_DATA_PATH") != other


def test_the_profile_can_turn_native_wayland_off(make_backend_context, tmp_path, monkeypatch):
    backend = _backend(
        make_backend_context, tmp_path, monkeypatch, {"PROTON_ENABLE_WAYLAND": "0"}
    )
    _argv, env = backend._prefix_command(["game.exe"])
    assert env["PROTON_ENABLE_WAYLAND"] == "0"


def test_installers_still_run_on_xwayland_whatever_the_profile_says(
    make_backend_context, tmp_path, monkeypatch
):
    backend = _backend(
        make_backend_context, tmp_path, monkeypatch, {"PROTON_ENABLE_WAYLAND": "1"}
    )
    _argv, env = backend._prefix_command(["setup.exe"], extra_env={"PROTON_ENABLE_WAYLAND": "0"})
    assert env["PROTON_ENABLE_WAYLAND"] == "0"  # the WebView2 installer breaks on Wayland


def test_an_internal_value_beats_the_profiles_for_the_same_variable(
    make_backend_context, tmp_path, monkeypatch
):
    backend = _backend(make_backend_context, tmp_path, monkeypatch, {"MY_VAR": "profile"})
    _argv, env = backend._prefix_command(["x.exe"], extra_env={"MY_VAR": "internal"})
    assert env["MY_VAR"] == "internal"


def test_the_game_keeps_gameid_zero_unless_the_profile_names_one(
    make_backend_context, tmp_path, monkeypatch
):
    backend = _backend(make_backend_context, tmp_path, monkeypatch)
    assert backend._prefix_command(["g.exe"])[1]["GAMEID"] == "0"
    backend = _backend(make_backend_context, tmp_path, monkeypatch, {"GAMEID": "umu-123"})
    assert backend._prefix_command(["g.exe"])[1]["GAMEID"] == "umu-123"
