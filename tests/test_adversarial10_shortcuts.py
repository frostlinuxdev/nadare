# SPDX-License-Identifier: GPL-3.0-or-later
"""Shortcut collisions and damaged Steam files must preserve unrelated entries."""

from __future__ import annotations

import hashlib

import pytest

from nadare.core import shortcuts as sc


@pytest.mark.parametrize(
    "first,second", [("Foo Bar", "Foo-Bar"), ("ゲーム", "別ゲーム"), ("A_B", "A B")]
)
def test_colliding_profile_slugs_keep_both_shortcuts(tmp_path, first, second):
    apps = tmp_path / "apps"
    one = sc.install_desktop_entry(first, first, applications_dir=apps, argv=["nadare"])[0]
    before = one.read_bytes()
    two = sc.install_desktop_entry(second, second, applications_dir=apps, argv=["nadare"])[0]
    assert one != two and one.read_bytes() == before
    assert len(sc.installed_desktop_entries(applications_dir=apps)) == 2
    sc.remove_desktop_entries(second, applications_dir=apps)
    assert one.read_bytes() == before


@pytest.mark.parametrize("profile", ["Game", "ゲーム", "space name"])
def test_fallback_shortcut_name_already_owned_by_someone_else_is_not_overwritten(tmp_path, profile):
    apps = tmp_path / "apps"
    apps.mkdir()
    base = apps / f"nadare-{sc.slug(profile)}.desktop"
    fallback = (
        apps / f"nadare-{sc.slug(profile)}-{hashlib.sha1(profile.encode()).hexdigest()[:8]}.desktop"
    )
    sentinel = b"[Desktop Entry]\nName=My unrelated shortcut\n"
    base.write_bytes(sentinel)
    fallback.write_bytes(sentinel)
    try:
        sc.install_desktop_entry(profile, profile, applications_dir=apps, argv=["nadare"])
    except sc.ShortcutError:
        pass  # refusing the collision is allowed
    assert base.read_bytes() == sentinel
    assert fallback.read_bytes() == sentinel, "fallback name overwrote an unowned desktop file"


@pytest.mark.parametrize("cut", [1, 2, 3, 5, 10, 20])
def test_truncated_steam_file_is_refused_before_rewrite(tmp_path, monkeypatch, cut):
    monkeypatch.setattr(sc, "steam_running", lambda: False)
    path = tmp_path / "shortcuts.vdf"
    valid = sc.vdf_dumps(
        [{"AppName": "Foreign game", "Exe": "/foreign", "LaunchOptions": "", "tags": {}}]
    )
    damaged = valid[:-cut]
    path.write_bytes(damaged)
    with pytest.raises(sc.ShortcutError):
        sc.add_steam_shortcut(path, "g", "Game", ["/nadare"])
    assert path.read_bytes() == damaged


@pytest.mark.parametrize("title", ["Title\nExec=bad", "Title\rHidden=true", "100% title", "ゲーム"])
def test_title_cannot_add_desktop_keys(title):
    text = sc.desktop_entry("profile", title, ["/opt/space name/nadare"])
    assert len([line for line in text.splitlines() if line.startswith("Exec=")]) == 1
    assert "Hidden=true" not in text.splitlines()
    assert "X-Nadare-Profile=profile" in text
