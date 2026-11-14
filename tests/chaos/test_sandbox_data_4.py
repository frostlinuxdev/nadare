# SPDX-License-Identifier: GPL-3.0-or-later
"""A custom XDG_DATA_HOME puts the umu runtime outside the folder the Sandbox binds."""

from __future__ import annotations

from pathlib import Path

from nadare.core import gamesandbox


def _rw_binds(argv):
    flags = argv[1 : argv.index("--")]
    return [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == "--bind"]


def test_the_umu_runtime_under_a_custom_xdg_data_home_is_still_bound(tmp_path, monkeypatch):
    home = tmp_path / "home"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    custom = home / ".data"
    srt = (
        custom / "umu" / "steamrt4" / "pressure-vessel"
        / "libexec" / "steam-runtime-tools-0" / "srt-bwrap"
    )
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n")
    srt.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(custom))
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(custom),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    argv, _wrapped = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
    binds = _rw_binds(argv)
    print("custom XDG_DATA_HOME:", custom)
    print("writable binds:", binds)
    umu_root = custom / "umu"
    covered = any(
        Path(src) == umu_root or umu_root in Path(src).parents for src, _dst in binds
    )
    assert covered, (
        "the real umu runtime directory is hidden with the home and never bound back; "
        "only its srt-bwrap file is visible"
    )
