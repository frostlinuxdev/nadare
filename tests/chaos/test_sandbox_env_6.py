# SPDX-License-Identifier: GPL-3.0-or-later
"""A nonsense XDG_RUNTIME_DIR must not make bwrap hide the whole filesystem."""

from __future__ import annotations

from nadare.core import gamesandbox


def test_a_root_xdg_runtime_dir_is_not_used_as_a_tmpfs(tmp_path):
    home = tmp_path / "home"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": "/",
    }
    try:
        argv, _wrapped = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
    except (gamesandbox.BroadBindError, ValueError):
        return  # refusing it with a clear reason is correct too
    flags = argv[1 : argv.index("--")]
    tmpfs = [flags[i + 1] for i, f in enumerate(flags) if f == "--tmpfs"]
    print("XDG_RUNTIME_DIR:", env["XDG_RUNTIME_DIR"])
    print("tmpfs mounts:", tmpfs)
    assert "/" not in tmpfs, "a tmpfs over / leaves even /bin and the game absent from the Sandbox"
