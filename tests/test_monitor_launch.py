# SPDX-License-Identifier: GPL-3.0-or-later
"""The monitor setting reaches the game's environment on every Wine-based backend (#779)."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct
from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.backends.wine import WineBackend
from nadare.core import monitors
from nadare.core.backends import BackendError

VAR = "WAYLANDDRV_PRIMARY_MONITOR"


@pytest.fixture(autouse=True)
def _two_monitors(tmp_path, monkeypatch):
    root = tmp_path / "drm"
    for entry, mode in (("card0-DP-1", "2560x1440"), ("card0-DP-2", "1920x1080")):
        (root / entry).mkdir(parents=True)
        (root / entry / "status").write_text("connected\n")
        (root / entry / "modes").write_text(mode + "\n")
    monkeypatch.setattr(monitors, "DRM_ROOT", root)
    monkeypatch.setattr(CznBackend, "_download_runner", _offline)


def _offline(self, rid, latest):
    raise BackendError("offline")


def _wine(tmp_path, profile, default_monitor=None):
    wine = write_exe(tmp_path / "bin" / "wine")
    values = {"prefix": str(tmp_path / "p"), "wine_binary": str(wine), **profile}
    ctx = make_context(tmp_path, "wine", values)
    if default_monitor:
        ctx.config.data["default_monitor"] = default_monitor
    return WineBackend(ctx)._prefix_command(["x.exe"])[1]


def _direct(tmp_path, monkeypatch, profile, default_monitor=None):
    values = {"prefix": str(tmp_path / "pfx"), **profile}
    ctx = make_context(tmp_path, "direct-gfl2-haoplay", values)
    if default_monitor:
        ctx.config.data["default_monitor"] = default_monitor
    backend = direct.Gfl2HaoplayBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")
    return backend._prefix_command(["x.exe"])[1]


def _czn(tmp_path, monkeypatch, profile, default_monitor=None):
    proton = tmp_path / "MyProton"
    proton.mkdir(exist_ok=True)
    (proton / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
    values = {"prefix": str(tmp_path / "prefix"), "runner": str(proton), **profile}
    ctx = make_context(tmp_path, "stove-czn", values)
    if default_monitor:
        ctx.config.data["default_monitor"] = default_monitor
    return CznBackend(ctx)._prefix_command(["g.exe"])[1]


@pytest.fixture(params=["wine", "direct", "czn"])
def env_for(request, tmp_path, monkeypatch):
    def build(profile, default_monitor=None):
        if request.param == "wine":
            return _wine(tmp_path, profile, default_monitor)
        if request.param == "direct":
            return _direct(tmp_path, monkeypatch, profile, default_monitor)
        return _czn(tmp_path, monkeypatch, profile, default_monitor)

    return build


def test_the_games_monitor_reaches_the_game(env_for):
    assert env_for({"monitor": "DP-2"})[VAR] == "DP-2"


def test_no_choice_adds_nothing(env_for):
    assert VAR not in env_for({})


def test_the_global_default_reaches_games_without_their_own(env_for):
    assert env_for({}, default_monitor="DP-1")[VAR] == "DP-1"
    assert env_for({"monitor": "DP-2"}, default_monitor="DP-1")[VAR] == "DP-2"


def test_a_monitor_that_is_not_connected_is_not_passed_on(env_for):
    assert VAR not in env_for({"monitor": "HDMI-A-1"})


def test_an_explicit_environment_line_beats_the_setting(env_for):
    env = env_for({"monitor": "DP-2", "extra_env": {VAR: "DP-1"}})
    assert env[VAR] == "DP-1"


def test_the_setting_is_offered_next_to_environment_on_these_backends():
    for cls in (WineBackend, direct.Gfl2HaoplayBackend, CznBackend):
        keys = [o.key for o in cls.options]
        assert "monitor" in keys, cls
        assert keys.index("monitor") == keys.index("extra_env") + 1
