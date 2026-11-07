# SPDX-License-Identifier: GPL-3.0-or-later
"""The first save after an unsafe profile key was migrated keeps the migrated values (#868)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import paths
from nadare.core.config import Config


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def _config_with_unsafe_key(tmp_path: Path, *, other_first: bool = False) -> Path:
    old_tree = paths.profiles_dir() / "a" / "b"
    (old_tree / "prefix").mkdir(parents=True)
    (old_tree / "prefix" / "marker").write_text("x")
    profiles = {"a/b": {"backend": "wine", "prefix": str(old_tree / "prefix")}}
    if other_first:
        profiles = {"safe": {"backend": "wine"}, **profiles}
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"profiles": profiles, "active_profile": "a/b", "language": "en"})
    )
    return target


def test_the_prefix_stays_where_the_migration_moved_it(xdg):
    path = _config_with_unsafe_key(xdg)
    cfg = Config.load(path)
    moved = paths.profiles_dir() / "a-b" / "prefix"
    assert cfg.profiles["a-b"]["prefix"] == str(moved)
    cfg.save()
    assert cfg.profiles["a-b"]["prefix"] == str(moved)
    assert (moved / "marker").exists()
    again = Config.load(path)
    assert again.profiles["a-b"]["prefix"] == str(moved)
    assert json.loads(path.read_text())["profiles"]["a-b"]["prefix"] == str(moved)


def test_the_active_game_stays_active_when_another_profile_comes_first(xdg):
    path = _config_with_unsafe_key(xdg, other_first=True)
    cfg = Config.load(path)
    assert cfg.data["active_profile"] == "a-b"
    cfg.save()
    assert cfg.data["active_profile"] == "a-b"
    assert Config.load(path).data["active_profile"] == "a-b"


def test_a_safe_config_still_merges_an_edit_made_elsewhere(xdg):
    """The fix must not turn the merge with other windows off."""
    path = xdg / "config.json"
    path.write_text(json.dumps({"profiles": {"g": {"backend": "wine", "title": "G"}}}))
    ours = Config.load(path)
    theirs = Config.load(path)
    theirs.profiles["g"]["title"] = "Renamed elsewhere"
    theirs.save()
    ours.data["language"] = "fr"
    ours.save()
    assert Config.load(path).profiles["g"]["title"] == "Renamed elsewhere"
    assert Config.load(path).data["language"] == "fr"


def test_an_edit_made_elsewhere_after_the_migration_is_still_merged(xdg):
    path = _config_with_unsafe_key(xdg)
    ours = Config.load(path)
    ours.save()  # first save writes the migrated key and paths
    theirs = Config.load(path)
    theirs.profiles["a-b"]["title"] = "Set in another window"
    theirs.save()
    ours.profiles["a-b"]["note"] = "mine"
    ours.save()
    final = Config.load(path).profiles["a-b"]
    assert final["title"] == "Set in another window" and final["note"] == "mine"
