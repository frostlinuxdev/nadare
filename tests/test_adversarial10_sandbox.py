# SPDX-License-Identifier: GPL-3.0-or-later
"""Sandbox path aliases may not restore broad access to the home folder."""

from __future__ import annotations

import copy

import pytest

from nadare.core import gamesandbox as gs


@pytest.mark.parametrize("target", ["root", "home", "parent", "config"])
@pytest.mark.parametrize("linked", [False, True])
def test_broad_prefix_is_refused_even_via_link(tmp_path, target, linked):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".config"
    config.mkdir()
    folder = {"root": tmp_path.anchor, "home": home, "parent": tmp_path, "config": config}[target]
    if linked:
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        folder = alias
    with pytest.raises(gs.BroadBindError):
        gs.wrap(["game.exe"], {"HOME": str(home)}, prefix=folder, home=home)


@pytest.mark.parametrize("name", ["space game", "ゲーム", "a/b/c"])
def test_narrow_sandbox_keeps_original_argv_and_environment(tmp_path, name):
    home = tmp_path / "home"
    prefix = home / "Games" / name / "prefix"
    prefix.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "DISPLAY": ":99",
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    before = copy.deepcopy(env)
    original = ["runner", "game.exe", "literal;argument"]
    argv, result = gs.wrap(original, env, prefix=prefix, home=home)
    assert argv[argv.index("--") + 1 :] == original
    assert original == ["runner", "game.exe", "literal;argument"] and env == before
    assert result is not env


@pytest.mark.parametrize("runtime", ["/", "home", "parent"])
def test_broad_runtime_dir_does_not_hide_root_or_expose_home(tmp_path, runtime):
    home = tmp_path / "home"
    prefix = home / "Games" / "g" / "prefix"
    prefix.mkdir(parents=True)
    given = "/" if runtime == "/" else str(home if runtime == "home" else tmp_path)
    try:
        argv, _ = gs.wrap(
            ["game.exe"], {"HOME": str(home), "XDG_RUNTIME_DIR": given}, prefix=prefix, home=home
        )
    except gs.BroadBindError:
        return
    flags = argv[1 : argv.index("--")]
    for i, flag in enumerate(flags):
        if flag == "--tmpfs":
            assert flags[i + 1] != "/"
        if flag in ("--bind", "--ro-bind"):
            assert flags[i + 1] not in (str(home), str(tmp_path))
