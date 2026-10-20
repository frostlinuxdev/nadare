# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.shortcuts (no Qt, no network, no real HOME)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import shortcuts


def test_slug_cases() -> None:
    assert shortcuts.slug("My Game!") == "my-game"
    assert shortcuts.slug("Fire Emblem: Three Houses") == "fire-emblem-three-houses"
    assert shortcuts.slug("a--b") == "a-b"
    assert shortcuts.slug("  ...  ") == "game"
    assert shortcuts.slug("") == "game"


def test_desktop_entry_quotes_profile_and_argv() -> None:
    text = shortcuts.desktop_entry('my "game"', "My Game", ["/opt/my games/nadare"])
    assert 'Exec="/opt/my games/nadare" launch -p "my \\"game\\""' in text
    assert 'X-Nadare-Profile=my "game"' in text
    assert "Type=Application" in text
    assert "Categories=Game;" in text
    assert "Terminal=false" in text


def test_desktop_entry_doubles_percent() -> None:
    text = shortcuts.desktop_entry("game", "100% Game", ["nadare"])
    assert "Exec=nadare launch -p game" in text
    assert "Name=100% Game" in text


def test_install_and_remove_only_touch_owned(tmp_path: Path) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    foreign = apps / "nadare-x.desktop"
    foreign.write_text("[Desktop Entry]\nName=Foreign\n", encoding="utf-8")

    written = shortcuts.install_desktop_entry(
        "game", "Game", applications_dir=apps, argv=["nadare"]
    )
    assert written == [apps / "nadare-game.desktop"]
    assert written[0].exists()
    assert foreign.exists()

    removed = shortcuts.remove_desktop_entries("game", applications_dir=apps)
    assert removed == [apps / "nadare-game.desktop"]
    assert not (apps / "nadare-game.desktop").exists()
    assert foreign.exists()


def test_refresh_updates_moved_appimage_and_removes_deleted(tmp_path: Path) -> None:
    apps = tmp_path / "applications"
    shortcuts.install_desktop_entry(
        "game", "Game", applications_dir=apps, argv=["/old/nadare.AppImage"]
    )
    shortcuts.install_desktop_entry(
        "gone", "Gone", applications_dir=apps, argv=["/old/nadare.AppImage"]
    )

    result = shortcuts.refresh_desktop_entries(
        {"game": {"title": "Game"}}, applications_dir=apps, argv=["/new/nadare.AppImage"]
    )

    assert result["updated"] == [apps / "nadare-game.desktop"]
    assert result["removed"] == [apps / "nadare-gone.desktop"]
    text = (apps / "nadare-game.desktop").read_text(encoding="utf-8")
    assert "/new/nadare.AppImage" in text
    assert not (apps / "nadare-gone.desktop").exists()


def _entry(title: str) -> dict[str, object]:
    return {
        "AppName": title,
        "Exe": '"/opt/nadare.AppImage"',
        "StartDir": "/opt",
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "launch -p one",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "tags": {},
    }


def test_vdf_round_trip_two_entries_and_unicode() -> None:
    entries = [_entry("Game One"), _entry("ゲーム・ツー")]
    assert shortcuts.vdf_loads(shortcuts.vdf_dumps(entries)) == entries
    assert shortcuts.vdf_loads(b"") == []
    assert shortcuts.vdf_loads(b"not a vdf") == []


def test_add_steam_shortcut_idempotent_and_backup_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    backup = tmp_path / "shortcuts.vdf.nadare-bak"
    argv = ["/opt/nadare.AppImage"]

    assert shortcuts.add_steam_shortcut(vdf, "one", "Game One", argv) is True
    assert not backup.exists()  # nothing to back up on first write
    assert shortcuts.add_steam_shortcut(vdf, "one", "Game One", argv) is False

    assert shortcuts.add_steam_shortcut(vdf, "two", "Game Two", ["/opt/other"]) is True
    assert backup.exists()
    backup_before = backup.read_bytes()
    assert shortcuts.add_steam_shortcut(vdf, "three", "Game Three", ["/opt/third"]) is True
    assert backup.read_bytes() == backup_before


def test_add_steam_shortcut_refuses_when_steam_running(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: True)
    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.add_steam_shortcut(tmp_path / "shortcuts.vdf", "one", "Game", ["/opt/n"])


def test_launcher_argv_appimage() -> None:
    assert shortcuts.launcher_argv({"APPIMAGE": "/opt/n.AppImage"}, False) == [
        "/opt/n.AppImage"
    ]


def test_launcher_argv_flatpak() -> None:
    assert shortcuts.launcher_argv({}, True) == ["flatpak", "run", shortcuts.FLATPAK_ID]


def test_launcher_argv_which(monkeypatch) -> None:
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: "/usr/bin/nadare")
    assert shortcuts.launcher_argv({}, False) == ["/usr/bin/nadare"]


def test_launcher_argv_fallback(monkeypatch) -> None:
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: None)
    assert shortcuts.launcher_argv({}, False, exe="/py") == ["/py", "-m", "nadare"]


def test_find_steam_shortcut_files(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".local" / "share" / "Steam" / "userdata" / "42" / "config"
    config.mkdir(parents=True)
    found = shortcuts.find_steam_shortcut_files()
    assert config / "shortcuts.vdf" in found


def test_colliding_slugs_do_not_share_an_entry(tmp_path):
    apps = tmp_path / "apps"
    shortcuts.install_desktop_entry("Game", "A", applications_dir=apps, argv=["nadare"])
    shortcuts.install_desktop_entry("game", "B", applications_dir=apps, argv=["nadare"])
    assert len(list(apps.glob("nadare-*.desktop"))) == 2
    assert len(shortcuts.remove_desktop_entries("Game", applications_dir=apps)) == 1
    left = shortcuts.installed_desktop_entries(applications_dir=apps)
    assert [o for _, o in left] == ["game"]


def test_non_utf8_desktop_file_is_skipped(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "nadare-bad.desktop").write_bytes(b"\xff\xfe\x00")
    assert shortcuts.remove_desktop_entries("game", applications_dir=apps) == []
    assert shortcuts.installed_desktop_entries(applications_dir=apps) == []


def test_desktop_entry_newlines_cannot_inject_keys():
    text = shortcuts.desktop_entry("p", "T\nExec=evil\r\nX-Nadare-Profile=keep", ["nadare"])
    assert sum(1 for ln in text.splitlines() if ln.startswith("Exec=")) == 1
    assert sum(1 for ln in text.splitlines() if ln.startswith("X-Nadare-Profile=")) == 1
