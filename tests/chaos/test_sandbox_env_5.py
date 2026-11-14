# SPDX-License-Identifier: GPL-3.0-or-later
"""Loader variables in the launch environment must not be loaded by bubblewrap itself."""

from __future__ import annotations

from nadare.core import gamesandbox


def test_ld_preload_is_not_in_the_environment_that_starts_bwrap(tmp_path):
    home = tmp_path / "home"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LD_PRELOAD": str(tmp_path / "game-controlled.so"),
    }
    argv, wrapped = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
    print("LD_PRELOAD given:", env["LD_PRELOAD"])
    loader = {k: v for k, v in wrapped.items() if k.startswith("LD_")}
    print("environment used to start bwrap:", loader)
    print("bwrap flags:", argv[1 : argv.index("--")])
    assert "LD_PRELOAD" not in wrapped, (
        "bubblewrap is dynamically linked and starts before the Sandbox exists, so the "
        "loader runs this library on the host; it must be passed to the game with --setenv "
        "or dropped, never left in bwrap's own environment"
    )


def test_ld_preload_is_not_in_the_environment_that_starts_flatpak_spawn(tmp_path):
    """The Flatpak path starts a host shell and flatpak-spawn before its sandbox exists."""
    home = tmp_path / "home"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    env_dir = tmp_path / "run"
    env_dir.mkdir()
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LD_PRELOAD": str(tmp_path / "game-controlled.so"),
    }
    _command, returned = gamesandbox.wrap_flatpak(
        ["game.exe"], env, prefix=prefix, home=home, env_dir=env_dir
    )
    loader = {k: v for k, v in returned.items() if k.startswith("LD_")}
    print("environment used to start the host shell:", loader)
    assert "LD_PRELOAD" not in returned, (
        "the host /bin/sh and flatpak-spawn load the game's library before the Flatpak "
        "sandbox starts"
    )
