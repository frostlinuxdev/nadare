# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: an unusable lock folder must be a clean refusal.

A read-only ``$XDG_STATE_HOME/nadare/locks`` (a read-only home, a full/foreign
state tree) currently leaks ``PermissionError`` out of the core Remove API.
"""

from __future__ import annotations

import os

import pytest

from nadare.core import paths, removal
from nadare.core.backends import BackendError
from nadare.core.config import Config


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_remove_game_reports_an_unusable_lock_folder_instead_of_a_traceback(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.profiles["gone"].update(prefix=str(prefix), owned_trees=["prefix"])

    locks = paths.state_dir() / "locks"
    locks.mkdir(parents=True)
    locks.chmod(0o555)
    try:
        print("lock folder writable:", os.access(locks, os.W_OK))
        with pytest.raises(BackendError) as caught:
            removal.remove_game(
                cfg,
                "gone",
                delete_trees=["prefix"],
                applications_dir=tmp_path / "apps",
            )
        print("clean error:", caught.value)
    finally:
        locks.chmod(0o755)

    assert "gone" in cfg.profiles
    assert prefix.is_dir(), "nothing may be deleted when the lock cannot be taken"
