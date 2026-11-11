# SPDX-License-Identifier: GPL-3.0-or-later
"""Only this user's Steam blocks shortcut edits: another account's Steam cannot be closed by us."""

from __future__ import annotations

import os

from nadare.core import shortcuts


def _fake_proc(tmp_path, comm: str = "steam"):
    entry = tmp_path / "4242"
    entry.mkdir()
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")
    return tmp_path


def test_this_users_steam_counts(tmp_path):
    assert shortcuts.steam_running(proc_root=_fake_proc(tmp_path)) is True


def test_another_users_steam_does_not(tmp_path, monkeypatch):
    root = _fake_proc(tmp_path)
    real = os.getuid()
    monkeypatch.setattr(shortcuts.os, "getuid", lambda: real + 1)  # our files look foreign
    assert shortcuts.steam_running(proc_root=root) is False


def test_other_programs_do_not(tmp_path):
    assert shortcuts.steam_running(proc_root=_fake_proc(tmp_path, "steamwebhelper")) is False
