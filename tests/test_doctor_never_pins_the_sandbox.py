# SPDX-License-Identifier: GPL-3.0-or-later
"""`nadare doctor` never records or runs a bubblewrap Nadare did not record (#965, 2026-11-13).

The Sandbox pins the runtime's ``srt-bwrap`` the first time it really starts a game. The folder
that copy lives in is writable from inside the Sandbox, so a game could plant one; that is why a
diagnostic must only LOOK. The doctor called ``locate()`` with recording on: with no pins yet (no
Sandboxed launch so far, or the pins file deleted as the docs advise after a runtime update) it
pinned and executed whatever was on disk.
"""

from __future__ import annotations

import os
import stat

from nadare import app
from nadare.core import gamesandbox, sandbox


def test_doctor_neither_pins_nor_runs_an_unrecorded_runtime_copy(tmp_path, monkeypatch, capsys):
    planted = tmp_path / "umu" / "srt-bwrap"
    planted.parent.mkdir(parents=True)
    ran = tmp_path / "planted-ran"
    planted.write_text(f"#!/bin/sh\ntouch {ran}\nexit 0\n")
    planted.chmod(planted.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: [str(planted)])
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, *_a, **_k: None)  # no bwrap
    assert not gamesandbox.pins_path().exists()

    app._print_sandbox_status()

    out = capsys.readouterr().out
    print(out)
    assert not ran.exists(), "the doctor executed a bubblewrap Nadare never recorded"
    assert not gamesandbox.pins_path().exists(), "the doctor recorded the runtime's copies"
    assert "did not record" in out or "unavailable" in out.lower()


def test_the_first_real_sandbox_start_still_records(tmp_path, monkeypatch):
    """The allowed side: a real launch (``locate()`` from the launch path) pins on first use."""
    copy = tmp_path / "umu" / "srt-bwrap"
    copy.parent.mkdir(parents=True)
    copy.write_text("#!/bin/sh\nexit 0\n")
    copy.chmod(copy.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: [str(copy)])
    monkeypatch.setattr(gamesandbox, "works", lambda binary=None: (True, ""))
    located = gamesandbox.locate()
    assert located.binary == str(copy)
    assert gamesandbox.pins_path().exists()
    assert os.fspath(copy) in gamesandbox.pins_path().read_text()
