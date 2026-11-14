# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F4: adding after a title edit appends a duplicate Steam shortcut."""
from __future__ import annotations

import pytest

from nadare.core import shortcuts


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)


def test_adding_the_same_profile_with_a_new_title_renames_instead_of_duplicating(tmp_path):
    vdf = tmp_path / "shortcuts.vdf"
    argv = ["/usr/bin/nadare"]
    assert shortcuts.add_steam_shortcut(vdf, "epic-seven", "Epic Seven", argv)
    assert shortcuts.add_steam_shortcut(vdf, "epic-seven", "Epic Seven (Steam)", argv)

    entries = shortcuts.vdf_loads(vdf.read_bytes(), strict=True)
    mine = [e for e in entries if "epic-seven" in str(e.get("LaunchOptions", ""))]
    print("entries for epic-seven:", [(e.get("AppName"), e.get("LaunchOptions")) for e in mine])
    assert len(mine) == 1, "one game now has two Steam shortcuts"
    assert mine[0]["AppName"] == "Epic Seven (Steam)"
