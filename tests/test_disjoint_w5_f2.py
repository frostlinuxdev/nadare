# SPDX-License-Identifier: GPL-3.0-or-later
"""W5-F2: one install folder, two sync implementations, two different lock files."""

from __future__ import annotations

import pytest

from nadare.core import directdl, yostar


def test_a_yostar_sync_waits_for_a_direct_sync_on_the_same_folder(tmp_path):
    install_dir = tmp_path / "shared-install"
    install_dir.mkdir()

    with directdl._install_lock(install_dir):
        # A Yostar sync into the very same tree must not start beside it (both
        # write ``.part`` files there).  Correct: the second acquisition is refused.
        with pytest.raises(yostar.SyncBusy):
            with yostar._install_lock(install_dir):
                pass
