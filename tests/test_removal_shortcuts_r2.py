# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins: removal refuses a tree nested inside another profile's tree; desktop Exec escaping."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from nadare.core import removal, shortcuts
from nadare.core.config import Config


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    return cfg, tmp_path


def test_a_tree_nested_inside_another_profiles_tree_is_refused(cfg_env):
    cfg, tmp = cfg_env
    parent = tmp / "Games" / "keep-game"
    nested = parent / "gone-prefix"
    nested.mkdir(parents=True)
    (nested / "system.reg").write_text("x")
    cfg.profiles["keep"]["install_dir"] = str(parent)
    cfg.profiles["gone"]["prefix"] = str(nested)
    result = removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )
    assert result.trees_removed == []
    assert nested.exists()
    assert any("keep" in w for w in result.warnings), result.warnings
    trees = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert trees["prefix"].safe is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("100%$HOME game", 'Exec=/opt/nadare launch -p "100%%\\$HOME game"'),
        ("a%b", "Exec=/opt/nadare launch -p a%%b"),
        ("x`id`y", 'Exec=/opt/nadare launch -p "x\\`id\\`y"'),
        ("back\\slash", 'Exec=/opt/nadare launch -p "back\\\\slash"'),
    ],
)
def test_desktop_exec_escapes_field_codes_and_shell_metachars(name, expected):
    text = shortcuts.desktop_entry(name, "T", ["/opt/nadare"])
    assert expected in text.splitlines()


def test_desktop_copy_is_executable_and_menu_copy_is_not(tmp_path: Path):
    apps, desk = tmp_path / "apps", tmp_path / "desk"
    apps.mkdir()
    desk.mkdir()
    menu, on_desktop = shortcuts.install_desktop_entry(
        "game", "Game", applications_dir=apps, desktop_dir=desk, argv=["nadare"]
    )
    assert stat.S_IMODE(on_desktop.stat().st_mode) & 0o111 == 0o111
    assert stat.S_IMODE(menu.stat().st_mode) & 0o111 == 0
