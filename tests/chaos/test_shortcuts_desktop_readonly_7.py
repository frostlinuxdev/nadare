# SPDX-License-Identifier: GPL-3.0-or-later
"""A refresh that cannot rewrite an entry must not report success.

``refresh_desktop_entries`` swallows every OSError from the atomic rewrite and
returns an empty ``updated`` list with no warning. On a read-only or otherwise
unwritable ``~/.local/share/applications`` the old Exec (a moved AppImage) stays
in place, but ``nadare shortcut refresh`` exits 0 and says nothing.
"""

from __future__ import annotations

import os

from nadare.core import shortcuts


def test_refresh_does_not_silently_skip_an_unwritable_entry(tmp_path):
    apps = tmp_path / "applications"
    shortcuts.install_desktop_entry("game", "Game", applications_dir=apps, argv=["/old/nadare"])
    entry = apps / "nadare-game.desktop"

    os.chmod(apps, 0o500)  # read + search, no write: the atomic replace must fail
    try:
        try:
            result = shortcuts.refresh_desktop_entries(
                {"game": {"title": "Game"}},
                applications_dir=apps,
                argv=["/new/nadare"],
            )
        except OSError:
            return  # reporting the failure instead of swallowing it is correct too

        print("result:", result)
        lines = entry.read_text().splitlines()
        print("Exec still:", [ln for ln in lines if ln.startswith("Exec=")])
        assert result["updated"] or result.get("warnings"), (
            "the entry was not refreshed but refresh returned no update and no warning"
        )
    finally:
        os.chmod(apps, 0o755)
