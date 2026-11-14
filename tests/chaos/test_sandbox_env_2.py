# SPDX-License-Identifier: GPL-3.0-or-later
"""XAUTHORITY from the launch environment must not bind the home or / back."""

from __future__ import annotations

import pytest

from nadare.core import gamesandbox


def _ro_binds(argv):
    flags = argv[1 : argv.index("--")]
    return [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == "--ro-bind"]


@pytest.mark.parametrize("value", ["home", "/"])
def test_a_broad_xauthority_does_not_re_expose_the_hidden_home(tmp_path, value):
    home = tmp_path / "home"
    prefix = home / "prefix"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("private key")
    prefix.mkdir()
    xauthority = str(home) if value == "home" else value
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XAUTHORITY": xauthority,
    }
    try:
        argv, _wrapped = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
    except (gamesandbox.BroadBindError, ValueError):
        return  # refusing the nonsense value is the other correct answer
    binds = _ro_binds(argv)
    print("XAUTHORITY:", xauthority)
    print("read-only binds:", binds)
    exposed = [src for src, _dst in binds if src in {str(home.resolve()), "/"}]
    assert exposed == [], (
        f"XAUTHORITY={xauthority!r} put {exposed} back on top of the hidden home"
    )
