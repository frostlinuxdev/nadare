# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-game shortcut icons (#1876 item 4).

A menu entry or Steam shortcut used Nadare's icon for every game. Now the game's own art is
cached as a 256x256 PNG and the shortcut points at it. Natively the PNG goes in the user's
hicolor theme; a Flatpak cannot write there, so it uses its own data folder, which the host
sees at the same path. The icon goes when the game's last shortcut goes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtGui")
from PyQt6.QtGui import QColor, QImage  # noqa: E402

from nadare.core import gameicons, sandbox, shortcuts  # noqa: E402

ARGV = ["/opt/nadare.AppImage"]


def _image(path: Path, width: int, height: int, fmt: str = "PNG") -> Path:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(200, 30, 30))
    assert image.save(str(path), fmt)
    return path


def _data_home() -> Path:
    return Path(os.environ["XDG_DATA_HOME"])


def _native_icon(profile: str) -> Path:
    return _data_home() / "icons/hicolor/256x256/apps" / f"nadare-{shortcuts.slug(profile)}.png"


def _icon_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("Icon="))


@pytest.fixture
def apps(tmp_path):
    folder = tmp_path / "applications"
    folder.mkdir()
    return folder


def _install(apps: Path, profile: str, data: dict) -> str:
    profiles = {profile: data}
    [entry] = shortcuts.install_desktop_entry(
        profile, data.get("title", profile), applications_dir=apps, argv=ARGV, profiles=profiles
    )
    return entry.read_text(encoding="utf-8")


def _png_size(path: Path) -> tuple[int, int]:
    image = QImage(str(path))
    assert not image.isNull()
    return image.width(), image.height()


def test_menu_entry_uses_the_games_icon_art(tmp_path, apps):
    art = _image(tmp_path / "czn-icon.png", 512, 512)
    text = _install(apps, "czn", {"title": "Chaos Zero Nightmare", "icon": str(art)})
    icon = _native_icon("czn")
    assert _icon_line(text) == f"Icon={icon}"
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert _png_size(icon) == (256, 256)


def test_a_portrait_cover_becomes_a_square_icon(tmp_path, apps):
    cover = _image(tmp_path / "cover.jpg", 600, 900, "JPG")
    text = _install(apps, "wuwa", {"title": "Wuthering Waves", "cover": str(cover)})
    assert _icon_line(text) == f"Icon={_native_icon('wuwa')}"
    assert _png_size(_native_icon("wuwa")) == (256, 256)


def test_icon_art_wins_over_the_cover(tmp_path, apps):
    icon_art = _image(tmp_path / "icon.png", 64, 64)
    cover = _image(tmp_path / "cover.png", 600, 900)
    assert gameicons.source_image({"icon": str(icon_art), "cover": str(cover)}) == icon_art


def test_without_art_the_entry_keeps_nadares_icon(apps):
    text = _install(apps, "plain", {"title": "Plain"})
    assert _icon_line(text) == "Icon=nadare"
    assert not _native_icon("plain").exists()


def test_art_that_is_not_an_image_keeps_nadares_icon(tmp_path, apps):
    bad = tmp_path / "icon.png"
    bad.write_bytes(b"<html>not found</html>")
    text = _install(apps, "bad", {"title": "Bad", "icon": str(bad)})
    assert _icon_line(text) == "Icon=nadare"
    assert not _native_icon("bad").exists()


def test_flatpak_writes_the_icon_where_the_host_can_see_it(tmp_path, apps, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    art = _image(tmp_path / "icon.png", 128, 128)
    text = _install(apps, "hsr", {"title": "Honkai: Star Rail", "icon": str(art)})
    icon = _data_home() / "nadare" / "shortcut-icons" / "nadare-hsr.png"
    assert _icon_line(text) == f"Icon={icon}"
    assert icon.is_file()
    assert not _native_icon("hsr").exists()


def test_refresh_keeps_the_games_icon(tmp_path, apps):
    art = _image(tmp_path / "icon.png", 128, 128)
    data = {"title": "Game", "icon": str(art)}
    _install(apps, "game", data)
    shortcuts.refresh_desktop_entries({"game": data}, applications_dir=apps, argv=["/opt/new"])
    text = (apps / "nadare-game.desktop").read_text(encoding="utf-8")
    assert _icon_line(text) == f"Icon={_native_icon('game')}"


def test_removing_the_menu_entry_removes_the_icon(tmp_path, apps, monkeypatch):
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [])
    _install(apps, "game", {"title": "Game", "icon": str(_image(tmp_path / "i.png", 64, 64))})
    assert _native_icon("game").is_file()
    shortcuts.remove_desktop_entries("game", applications_dir=apps)
    assert not _native_icon("game").exists()


def test_the_icon_stays_while_a_steam_shortcut_uses_it(tmp_path, apps, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    data = {"title": "Game", "icon": str(_image(tmp_path / "i.png", 64, 64))}
    _install(apps, "game", data)
    shortcuts.add_steam_shortcut(vdf, "game", "Game", ARGV, profiles={"game": data})
    shortcuts.remove_desktop_entries("game", applications_dir=apps)
    assert _native_icon("game").is_file()
    shortcuts.remove_steam_shortcuts("game")
    assert not _native_icon("game").exists()


def test_steam_shortcut_gets_the_games_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    data = {"title": "Game", "icon": str(_image(tmp_path / "i.png", 64, 64))}
    assert shortcuts.add_steam_shortcut(vdf, "game", "Game", ARGV, profiles={"game": data})
    [entry] = shortcuts.vdf_loads(vdf.read_bytes())
    assert entry["icon"] == str(_native_icon("game"))


def test_steam_shortcut_without_art_has_no_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    assert shortcuts.add_steam_shortcut(vdf, "g", "G", ARGV, profiles={"g": {"title": "G"}})
    [entry] = shortcuts.vdf_loads(vdf.read_bytes())
    assert entry["icon"] == ""


def test_an_older_steam_entry_gains_the_icon_when_added_again(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    assert shortcuts.add_steam_shortcut(vdf, "game", "Game", ARGV, profiles={"game": {}})
    data = {"title": "Game", "icon": str(_image(tmp_path / "i.png", 64, 64))}
    assert shortcuts.add_steam_shortcut(vdf, "game", "Game", ARGV, profiles={"game": data})
    [entry] = shortcuts.vdf_loads(vdf.read_bytes())
    assert entry["icon"] == str(_native_icon("game"))


def test_a_newer_art_file_refreshes_the_icon(tmp_path, apps):
    art = _image(tmp_path / "i.png", 64, 64)
    data = {"title": "Game", "icon": str(art)}
    _install(apps, "game", data)
    before = _native_icon("game").read_bytes()
    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(QColor(10, 200, 10))
    assert image.save(str(art), "PNG")
    os.utime(art, (os.stat(art).st_mtime + 10,) * 2)
    _install(apps, "game", data)
    assert _native_icon("game").read_bytes() != before


def test_shortcuts_module_still_has_no_qt_import():
    source = Path(shortcuts.__file__).read_text(encoding="utf-8")
    assert "PyQt6" not in source
