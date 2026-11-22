# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F1: two advanced games share one fixed title, so shortcuts/art collide."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core import artwork, shortcuts
from nadare.core.config import Config


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_two_steam_games_get_distinct_shortcut_names_and_art_keys(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    names = []
    for profile, app_id in (("steam-a", "5019180"), ("steam-b", "3557620")):
        # Both carry the same fixed title, as the old wizard's "Steam game" row gave them.
        config.add_profile(profile, backend="steam", title="Steam game", steam_app_id=app_id)
        names.append(profile)

    titles = [config.profiles[n]["title"] for n in names]
    first = shortcuts.desktop_entry(names[0], titles[0], ["/usr/bin/nadare"])
    second = shortcuts.desktop_entry(names[1], titles[1], ["/usr/bin/nadare"])
    name_a = next(line for line in first.splitlines() if line.startswith("Name="))
    name_b = next(line for line in second.splitlines() if line.startswith("Name="))
    print("titles:", titles, "desktop names:", name_a, name_b)
    assert name_a != name_b, "two games show the same menu name"

    slug_a = artwork.slug(config.profiles[names[0]])
    slug_b = artwork.slug(config.profiles[names[1]])
    print("artwork slugs:", slug_a, slug_b)
    assert slug_a != slug_b, "two games share one artwork cache key"
