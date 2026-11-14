# SPDX-License-Identifier: GPL-3.0-or-later
"""Adding a shortcut must not silently rewrite another Steam shortcut's bytes.

``vdf_loads`` decodes C strings with ``errors="replace"``. A shortcuts.vdf that
contains non-UTF-8 bytes (an older Steam/Wine file, or one edited on Windows) is
therefore accepted as "valid", and the next add/remove re-encodes the replaced
characters, corrupting the user's other shortcuts.
"""

from __future__ import annotations

from nadare.core import shortcuts

FOREIGN = {
    "AppName": "Café",
    "Exe": '"/opt/other-launcher/start"',
    "StartDir": "/opt/other-launcher",
    "icon": "",
    "ShortcutPath": "",
    "LaunchOptions": "",
    "IsHidden": 0,
    "AllowDesktopConfig": 1,
    "AllowOverlay": 1,
    "OpenVR": 0,
    "Devkit": 0,
    "DevkitOverrideAppID": 0,
    "LastPlayTime": 0,
    "tags": {},
}


def test_a_non_utf8_title_in_the_users_vdf_survives_adding_a_shortcut(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    # Steam stores Windows-1252/Latin-1 here, not UTF-8.
    raw = shortcuts.vdf_dumps([FOREIGN]).replace(b"Caf\xc3\xa9", b"Caf\xe9")
    vdf.write_bytes(raw)

    try:
        added = shortcuts.add_steam_shortcut(vdf, "game", "Game", ["/opt/nadare"])
    except shortcuts.ShortcutError as exc:
        # Refusing with a clear message and leaving the file alone is also correct.
        print("refused:", exc)
        assert vdf.read_bytes() == raw
        return

    after = vdf.read_bytes()
    print("added:", added)
    print("non-UTF-8 byte still present:", b"Caf\xe9" in after)
    print("replacement characters written:", b"Caf\xef\xbf\xbd" in after)
    assert b"Caf\xe9" in after, (
        "adding Nadare's shortcut rewrote a foreign shortcut whose title was not "
        "valid UTF-8, replacing the user's bytes with U+FFFD"
    )
