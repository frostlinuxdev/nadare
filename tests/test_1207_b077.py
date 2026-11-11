# SPDX-License-Identifier: GPL-3.0-or-later
"""A legacy profile saved with the home folder as its prefix must not claim every game (#851)."""

from pathlib import Path

from nadare.core import prefixguard


def test_a_legacy_home_prefix_does_not_block_a_new_game_folder_under_home():
    home = Path.home()
    # This state is real: Settings keeps an unchanged bad prefix, and #851 exists precisely
    # because profiles saved with home/a drive as the prefix were around before the guard.
    profiles = {"legacy": {"backend": "wine", "title": "Legacy", "prefix": str(home)}}
    assert (
        prefixguard.unsafe_reason(home / "Games" / "new" / "prefix", profiles=profiles, own="new")
        == ""
    )
