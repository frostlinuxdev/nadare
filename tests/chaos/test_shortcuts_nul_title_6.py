# SPDX-License-Identifier: GPL-3.0-or-later
"""Adding the same Steam shortcut twice must stay idempotent.

``_cstr`` drops NUL from values (so a title cannot fake another VDF key), but
the duplicate check compares the raw title with the title read back. A title
containing NUL therefore never matches, and every add appends another copy of
the same shortcut.
"""

from __future__ import annotations

from nadare.core import shortcuts


def test_a_nul_in_the_title_does_not_make_add_append_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    title = "Game\x00Injected=1"
    argv = ["/opt/nadare"]

    added = shortcuts.add_steam_shortcut(vdf, "game", title, argv)
    print("first add:", added)
    assert added is True
    try:
        second = shortcuts.add_steam_shortcut(vdf, "game", title, argv)
    except (shortcuts.ShortcutError, ValueError):
        return  # rejecting a NUL title with a clear message is acceptable

    entries = shortcuts.vdf_loads(vdf.read_bytes(), strict=True)
    print("second add:", second)
    print("entries:", [(e.get("AppName"), e.get("LaunchOptions")) for e in entries])
    assert second is False
    assert len(entries) == 1
