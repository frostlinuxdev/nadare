# SPDX-License-Identifier: GPL-3.0-or-later
"""A hostile launch PATH must not choose which bubblewrap Nadare runs."""

from __future__ import annotations

from pathlib import Path

from nadare.core import gamesandbox


def _pairs(argv, flag):
    flags = argv[1 : argv.index("--")]
    return [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == flag]


def test_the_system_bubblewrap_is_pinned_to_the_one_that_was_probed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    fake = tmp_path / "games" / "Hostile"
    fake.mkdir(parents=True)
    hijack = fake / "bwrap"
    hijack.write_text("#!/bin/sh\necho not the real bwrap\n")
    hijack.chmod(0o755)

    real_which = gamesandbox.shutil.which
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: [])
    monkeypatch.setattr(
        gamesandbox.shutil, "which",
        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    found = gamesandbox.locate()
    assert found.binary is not None, "fixture: a working copy is chosen"

    env = {
        "HOME": str(home),
        "PATH": f"{fake}:/usr/bin:/bin",
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    argv, _wrapped = gamesandbox.wrap(
        ["game.exe"], env, prefix=prefix, home=home, bwrap=found.binary
    )
    would_run = real_which("bwrap", path=env["PATH"])
    print("locate() chose:", repr(found.binary))
    print("command[0]:", repr(argv[0]))
    print("profile PATH would run:", would_run)
    assert would_run == str(hijack), "fixture: the hostile PATH entry comes first"
    assert Path(argv[0]).is_absolute() and argv[0] == "/usr/bin/bwrap", (
        "the launch resolves a bare 'bwrap' through the profile-controlled PATH, so a "
        "different binary than the one locate()/works() checked runs outside the Sandbox"
    )
