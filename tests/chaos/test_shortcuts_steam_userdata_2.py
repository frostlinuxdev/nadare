# SPDX-License-Identifier: GPL-3.0-or-later
"""One unreadable Steam userdata folder must not block the readable ones.

A shortcuts.vdf that cannot be listed (permissions, a broken mount) is skipped
by Steam and by ``find_steam_shortcut_files``'s callers' error handling, but the
discovery loop itself lets PermissionError escape. Removing a game then fails
before even reaching the readable Steam account's file.
"""

from __future__ import annotations

import os
from pathlib import Path

from nadare.core import shortcuts

ENTRY = {
    "AppName": "Work",
    "Exe": '"/opt/nadare.AppImage"',
    "StartDir": "/opt",
    "icon": "",
    "ShortcutPath": "",
    "LaunchOptions": 'launch -p "work"',
    "IsHidden": 0,
    "AllowDesktopConfig": 1,
    "AllowOverlay": 1,
    "OpenVR": 0,
    "Devkit": 0,
    "DevkitOverrideAppID": 0,
    "LastPlayTime": 0,
    "tags": {"0": shortcuts.STEAM_TAG},
}


def test_an_unreadable_userdata_folder_does_not_hide_a_readable_one(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    home = Path(os.environ["HOME"])
    readable = home / ".steam" / "steam" / "userdata" / "42" / "config"
    readable.mkdir(parents=True)
    vdf = readable / "shortcuts.vdf"
    vdf.write_bytes(shortcuts.vdf_dumps([ENTRY]))

    blocked = home / ".local" / "share" / "Steam" / "userdata"
    blocked.mkdir(parents=True)
    os.chmod(blocked, 0o000)
    try:
        print("blocked folder:", blocked, oct(blocked.stat().st_mode & 0o777))
        found = shortcuts.find_steam_shortcut_files()
        print("found:", sorted(str(p) for p in found))
        assert vdf in found

        changed, warnings = shortcuts.remove_steam_shortcuts("work")
        print("changed:", changed)
        print("warnings:", warnings)
        assert vdf in changed
        assert shortcuts.vdf_loads(vdf.read_bytes(), strict=True) == []
    finally:
        os.chmod(blocked, 0o755)
