# SPDX-License-Identifier: GPL-3.0-or-later
"""Review 2026-11-09: the game page's Sandbox question never pins or runs an unrecorded copy.

``ready()`` (asked when a game page opens) called ``locate()``, whose first call records every
``srt-bwrap`` on disk as trusted and then runs one: a copy a game planted before the Sandbox's
first real start would have been trusted by merely opening a page (the case #965 fixed for
``doctor``). The answer is also refreshed when the recorded copies change, so the page does not
stay wrong for the whole run.
"""

from __future__ import annotations

import os

from nadare.core import gamesandbox


def _runtime_copy(tmp_path, monkeypatch, name="srt-bwrap"):
    copy = tmp_path / "rt" / name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("#!/bin/sh\nexit 0\n")
    copy.chmod(0o755)
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: [str(copy)])
    return copy


def test_asking_ready_records_nothing_and_runs_no_unrecorded_copy(tmp_path, monkeypatch):
    copy = _runtime_copy(tmp_path, monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (
        ran.append(binary) or (True, "")))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    assert gamesandbox.ready() is False
    assert not gamesandbox.pins_path().exists()
    assert str(copy) not in ran


def test_a_recorded_copy_counts(tmp_path, monkeypatch):
    _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    gamesandbox.trusted_runtime_copies()  # what the Sandbox's first real start does
    assert gamesandbox.ready() is True


def test_the_answer_follows_a_change_of_the_recorded_copies(tmp_path, monkeypatch):
    _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    assert gamesandbox.ready() is False  # nothing recorded yet
    gamesandbox.trusted_runtime_copies()  # the first sandboxed launch records it
    stamp = os.stat(gamesandbox.pins_path()).st_mtime_ns + 1_000_000
    os.utime(gamesandbox.pins_path(), ns=(stamp, stamp))
    assert gamesandbox.ready() is True  # no restart needed


def test_the_launch_path_still_records_on_first_use(tmp_path, monkeypatch):
    copy = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    assert gamesandbox.locate().binary == str(copy)
    assert gamesandbox.pins_path().exists()


def test_a_game_page_without_the_sandbox_never_probes(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gamesandbox, "ready", lambda: pytest.fail("probed for a Steam game"))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("s", backend="steam", title="S")
    window = MainWindow(config)
    try:
        window._sync_sandbox("s")
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
