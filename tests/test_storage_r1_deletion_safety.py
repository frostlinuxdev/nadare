# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage review round 1 (Flash, 2026-11-14): nothing outside Nadare's own folders is deleted.

B1: ``profiles/<name>`` as a link to e.g. Documents made Uninstall empty the link's target.
B2: a planted manifest made the b21 ``Nadare/Nadare`` library container one "game".
A1: Move deleted an original Nadare never made (a shared ``~/.wine``).
A2: a link inside a moved tree still pointed at the old place, which was then deleted.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from nadare.core import libraries, paths, relocate, removal
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home


# ---- B1 ----------------------------------------------------------------------------------
@pytest.mark.parametrize("configured", [False, True])
def test_uninstall_never_deletes_through_a_linked_profiles_folder(env, tmp_path, configured):
    cfg, home = env
    documents = home / "Documents"
    (documents / "prefix" / "drive_c").mkdir(parents=True)
    (documents / "prefix" / "thesis.txt").write_text("years of work")
    paths.profiles_dir().mkdir(parents=True, exist_ok=True)
    (paths.profiles_dir() / "gone").symlink_to(documents)
    values = {"backend": "wine", "title": "Gone"}
    if configured:
        values["prefix"] = str(paths.profiles_dir() / "gone" / "prefix")
    cfg.add_profile("gone", **values)
    cfg.save()
    offered = [t for t in removal.removable_trees(cfg, "gone") if t.safe]
    assert offered == []
    removal.remove_game(cfg, "gone", delete_trees=["prefix"], applications_dir=tmp_path / "a")
    assert (documents / "prefix" / "thesis.txt").read_text() == "years of work"


# ---- B2 ----------------------------------------------------------------------------------
def _b21_library(home: Path) -> Path:
    container = home / "Games" / libraries.NADARE_DIR / libraries.NADARE_DIR
    for game in ("Game A", "Game B"):
        (container / game / "install").mkdir(parents=True)
        (container / game / "install" / "game.exe").write_text("x")
    return container


def test_a_manifest_in_a_nested_nadare_folder_cannot_claim_the_games_below(env):
    cfg, home = env
    container = _b21_library(home)
    (container / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Innocent Game",
                "install": "Game A",
                "made_by_nadare": ["install"],
            }
        )
    )
    manifest = libraries.read_manifest(container)
    assert manifest is not None and manifest.made_by_nadare == ()


@pytest.mark.parametrize("field", ["install", "prefix"])
def test_a_manifest_naming_its_own_folder_as_a_tree_is_refused(env, field):
    _cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Fake"
    (game_dir / "install").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Fake",
                field: ".",
                "made_by_nadare": [field],
            }
        )
    )
    assert libraries.read_manifest(game_dir) is None


# ---- A1 / A2 -----------------------------------------------------------------------------
def _prefix(root: Path) -> Path:
    (root / "drive_c" / "windows").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def test_an_absolute_link_into_the_original_keeps_the_original(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    (old / "data").mkdir()
    (old / "data" / "save.dat").write_text("save")
    (old / "alias").symlink_to(old / "data")  # absolute, inside the tree
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and (old / "data" / "save.dat").is_file()
    assert "link" in outcome.reason


def test_relative_links_inside_the_tree_are_fine(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    (old / "dosdevices").mkdir()
    os.symlink("../drive_c", old / "dosdevices" / "c:")
    os.symlink("/", old / "dosdevices" / "z:")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    assert relocate.remove_original(old, new).removed


def test_a_changed_link_target_counts_as_a_mismatch(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    os.symlink("../drive_c", old / "c")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    (new / "c").unlink()
    os.symlink("../elsewhere", new / "c")
    assert not relocate.remove_original(old, new).removed


def test_nadares_manifest_written_after_the_copy_does_not_block_removal(tmp_path):
    old = _prefix(tmp_path / "a" / "Game")
    new = tmp_path / "b" / "Game"
    shutil.copytree(old, new, symlinks=True)
    (new / libraries.MANIFEST_NAME).write_text("{}")
    assert relocate.remove_original(old, new).removed
