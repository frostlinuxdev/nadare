# SPDX-License-Identifier: GPL-3.0-or-later
"""Concurrent profile edits merge independent nested environment keys."""

from nadare.core.config import Config


def test_extra_env_merges_independent_edits_from_local_and_disk():
    snapshot = {"extra_env": {"DXVK_HUD": "0", "MANGOHUD": "0"}}
    local = {"extra_env": {"DXVK_HUD": "fps", "MANGOHUD": "0"}}
    disk = {"extra_env": {"DXVK_HUD": "0", "MANGOHUD": "1"}}

    assert Config._merge_profile(snapshot, local, disk) == {
        "extra_env": {"DXVK_HUD": "fps", "MANGOHUD": "1"}
    }
