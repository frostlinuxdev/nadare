# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: an unusable direct-download lock folder is a clean error."""

from __future__ import annotations

import os

import pytest

from nadare.core import directdl as dd
from nadare.core import paths


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_sync_reports_a_lock_folder_it_cannot_write_instead_of_a_traceback(tmp_path):
    locks = paths.state_dir() / "locks"
    locks.mkdir(parents=True)
    locks.chmod(0o555)
    try:
        print("lock folder writable:", os.access(locks, os.W_OK))
        with pytest.raises(dd.DirectError) as caught:
            dd.sync(dd.Plan("T", "1"), tmp_path / "install")
        print("clean error:", caught.value)
    finally:
        locks.chmod(0o755)
