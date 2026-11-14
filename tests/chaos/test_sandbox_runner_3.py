# SPDX-License-Identifier: GPL-3.0-or-later
"""A runner folder that is the home (or a private part of it) must not be bound back."""

from __future__ import annotations

import pytest

from nadare.core import gamesandbox


def _ro_binds(argv):
    flags = argv[1 : argv.index("--")]
    return [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == "--ro-bind"]


@pytest.mark.parametrize("which", ["home", "ssh"])
def test_a_broad_runner_folder_is_refused_instead_of_binding_the_home(tmp_path, which):
    home = tmp_path / "home"
    prefix = tmp_path / "prefix"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("private key")
    prefix.mkdir()
    runner = home if which == "home" else home / ".ssh"
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "data")}
    try:
        argv, _wrapped = gamesandbox.wrap(
            ["game.exe"], env, prefix=prefix, home=home, runner_dir=runner
        )
    except (gamesandbox.BroadBindError, ValueError):
        return  # the same clear refusal the prefix and game folder get
    binds = _ro_binds(argv)
    print("runner:", runner)
    print("read-only binds:", binds)
    target = str(runner.resolve())
    assert (target, target) not in binds, (
        f"runner={runner} was bound read-only, exposing the hidden home contents"
    )
