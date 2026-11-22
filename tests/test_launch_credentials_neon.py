# SPDX-License-Identifier: GPL-3.0-or-later
"""Credential names must not bypass launch filtering through an allowed prefix."""

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import script
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.core import process


@pytest.mark.parametrize("backend", ["script", "lepton", "build_env"])
@pytest.mark.parametrize("nested", [False, True])
def test_launch_drops_implicit_credentials(backend, nested, tmp_path, monkeypatch):
    secrets = dict.fromkeys(
        (
            "STEAM_API_KEY",
            "STEAM_PASSWORD",
            "NVIDIA_API_KEY",
            "AMD_TOKEN",
            "WINE_SECRET",
            "PROTON_ACCESS_KEY",
            "SDL_PRIVATE_KEY",
            "VK_APIKEY",
            "MESA_SECRET_KEY",
            "UMU_PASSWD",
            "DXVK_CREDENTIALS",
            "WINE_lower_token",
        ),
        "synthetic-host-value",
    )
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DISPLAY", ":19")
    monkeypatch.setenv("WINEDEBUG", "-all")
    monkeypatch.setenv("STEAM_RUNTIME", "1")
    settings = {
        "pass_env": ["AMD_TOKEN", "WINE_SECRET", "STEAM_*", "MISSING_TOKEN"],
        "extra_env": {"WINE_SECRET": "profile-choice", "PROTON_API_KEY": "explicit"},
    }
    profile = {"extra": settings} if nested else settings
    if backend == "script":
        source = write_exe(tmp_path / "game.sh", "#!/bin/sh\nexit 0\n")
        profile = {**profile, "launch_exe": str(source)}
        seen = []
        monkeypatch.setattr(script, "stream", lambda argv, env, **kw: seen.append(env) or 0)
        assert script.ScriptBackend(make_context(tmp_path, "script", profile)).launch() == 0
        env = seen[0]
    elif backend == "lepton":
        context = make_context(tmp_path, "fgo-lepton", profile)
        env = FgoLeptonBackend(context).adapter.env()
    else:
        env = process.build_env(profile, {"SDL_BACKEND_TOKEN": "backend-choice"})
        assert env["SDL_BACKEND_TOKEN"] == "backend-choice"

    assert not (secrets.keys() - {"AMD_TOKEN", "WINE_SECRET"}) & env.keys()
    assert env["AMD_TOKEN"] == "synthetic-host-value"
    assert env["WINE_SECRET"] == "profile-choice"
    assert env["PROTON_API_KEY"] == "explicit"
    assert "MISSING_TOKEN" not in env
    assert env["DISPLAY"] == ":19"
    assert env["WINEDEBUG"] == "-all"
    assert env["STEAM_RUNTIME"] == "1"
