# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare removes and refreshes only the Steam shortcuts it made (#893)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import shortcuts

FOREIGN = {
    "AppName": "Work tool",
    "Exe": '"/opt/other-launcher/start"',
    "StartDir": "/opt/other-launcher",
    "icon": "",
    "ShortcutPath": "",
    "LaunchOptions": '--mode custom launch -p "work"',
    "tags": {},
}


def _entry(exe: str, options: str, title: str = "Game") -> dict:
    return {
        "AppName": title,
        "Exe": f'"{exe}"',
        "StartDir": "/x",
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": options,
        "tags": {},
    }


NADARES = {
    "appimage": _entry("/home/u/Apps/Nadare-0.1.0b20-x86_64.AppImage", 'launch -p "work"'),
    "path": _entry("/usr/bin/nadare", 'launch -p "work"'),
    "flatpak": _entry("/usr/bin/flatpak", 'run io.github.frostlinuxdev.Nadare launch -p "work"'),
    "module": _entry("/usr/bin/python3", '-m nadare launch -p "work"'),
}


@pytest.fixture(autouse=True)
def steam_closed(monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)


def _write(path: Path, entries: list[dict]) -> Path:
    path.write_bytes(shortcuts.vdf_dumps(entries))
    return path


def _read(path: Path) -> list[dict]:
    return shortcuts.vdf_loads(path.read_bytes(), strict=True)


def test_a_foreign_entry_with_the_same_ending_survives_remove(tmp_path):
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN])
    assert shortcuts.remove_steam_shortcut(vdf, "work") is False
    assert [e["AppName"] for e in _read(vdf)] == ["Work tool"]


@pytest.mark.parametrize("kind", sorted(NADARES))
def test_every_way_nadare_makes_a_shortcut_is_removed(tmp_path, kind):
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN, NADARES[kind]])
    assert shortcuts.remove_steam_shortcut(vdf, "work") is True
    assert [e["AppName"] for e in _read(vdf)] == ["Work tool"]


def test_a_shortcut_nadare_just_added_is_removed_again(tmp_path):
    vdf = tmp_path / "shortcuts.vdf"
    argv = ["/home/u/Apps/Nadare-0.1.0b21-x86_64.AppImage"]
    shortcuts.add_steam_shortcut(vdf, "work", "Work", argv)
    assert shortcuts.remove_steam_shortcut(vdf, "work") is True
    assert _read(vdf) == []


def test_refresh_never_drops_a_foreign_entry(tmp_path, monkeypatch):
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN, NADARES["appimage"]])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    result = shortcuts.refresh_steam_shortcuts({})
    assert [e["AppName"] for e in _read(vdf)] == ["Work tool"]
    assert any("work" in item for item in result["removed"])


def test_refresh_does_not_retitle_a_foreign_entry(tmp_path, monkeypatch):
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts({"work": {"title": "Renamed by Nadare"}})
    assert _read(vdf)[0]["AppName"] == "Work tool"


def test_the_list_of_installed_shortcuts_skips_foreign_entries(tmp_path, monkeypatch):
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN, NADARES["appimage"]])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    assert [(p, t) for _f, p, t in shortcuts.installed_steam_shortcuts()] == [("work", "Game")]


# ---- a moved Nadare is repaired by add and by refresh (#899) -----------------------------------
def test_adding_again_from_a_new_path_repairs_the_executable(tmp_path):
    vdf = tmp_path / "shortcuts.vdf"
    assert shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/old/Nadare.AppImage"]) is True
    assert shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/new/Nadare.AppImage"]) is True
    (entry,) = _read(vdf)
    assert entry["Exe"] == '"/new/Nadare.AppImage"' and entry["StartDir"] == "/new"


def test_adding_again_from_the_same_path_changes_nothing(tmp_path):
    vdf = tmp_path / "shortcuts.vdf"
    shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/old/Nadare.AppImage"])
    before = vdf.read_bytes()
    assert shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/old/Nadare.AppImage"]) is False
    assert vdf.read_bytes() == before


def test_a_foreign_entry_with_the_same_title_is_never_repaired(tmp_path):
    foreign = dict(FOREIGN, AppName="Example", LaunchOptions='launch -p "game"')
    vdf = _write(tmp_path / "shortcuts.vdf", [foreign])
    shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/new/Nadare.AppImage"])
    entries = _read(vdf)
    assert entries[0]["Exe"] == '"/opt/other-launcher/start"'
    assert len(entries) == 2  # Nadare's own is added next to it


def test_refresh_repairs_a_moved_executable_too(tmp_path, monkeypatch):
    vdf = tmp_path / "shortcuts.vdf"
    shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/old/Nadare.AppImage"])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    result = shortcuts.refresh_steam_shortcuts(
        {"game": {"title": "Example"}}, argv=["/new/Nadare.AppImage"]
    )
    assert vdf in result["updated"]
    assert _read(vdf)[0]["Exe"] == '"/new/Nadare.AppImage"'


def test_refresh_without_a_launcher_path_leaves_the_executable(tmp_path, monkeypatch):
    vdf = tmp_path / "shortcuts.vdf"
    shortcuts.add_steam_shortcut(vdf, "game", "Example", ["/old/Nadare.AppImage"])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts({"game": {"title": "Example"}})
    assert _read(vdf)[0]["Exe"] == '"/old/Nadare.AppImage"'
