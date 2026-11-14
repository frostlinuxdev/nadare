# SPDX-License-Identifier: GPL-3.0-or-later
"""Removal must not delete through a symlinked game folder.

A game folder (or a library) can be replaced by a symlink: the game itself, a backup
tool, or the user moving things around. The final path component is not a symlink, but
its parent is. Removal resolves the path and deletes the target's contents, which are
not the files Nadare made.
"""

from __future__ import annotations

import os

from nadare.core import removal
from nadare.core.config import Config


def test_deleting_an_install_tree_does_not_follow_a_symlinked_game_folder(tmp_path):
    lib = tmp_path / "Games"
    game = lib / "Nadare" / "My Game"
    install = game / "install"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")

    # The user's own folder that the game folder now points at. It has an ``install``
    # subfolder with the same name, so a resolved deletion takes it.
    documents = tmp_path / "Documents"
    (documents / "install").mkdir(parents=True)
    (documents / "install" / "thesis.txt").write_text("my important files")

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "gone",
        backend="wine",
        title="My Game",
        install_dir=str(install),
        owned_dirs=[str(game)],
        owned_trees=["install_dir"],
        owned_paths={"install_dir": str(install.resolve())},
    )
    cfg.save()

    # Replace the real game folder with a link to Documents. The install path itself
    # (``.../My Game/install``) is not a symlink, only the folder above it is.
    os.rename(game, tmp_path / "moved-away")
    os.symlink(documents, game, target_is_directory=True)

    print("install is a symlink:", install.is_symlink())
    print("install resolves to:", install.resolve())
    print("tree safety:", removal.tree_safety(cfg.profiles, "gone", install))

    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp_path / "apps"
    )

    print("warnings:", result.warnings)
    print("trees_removed:", result.trees_removed)
    print("thesis survived:", (documents / "install" / "thesis.txt").exists())

    assert (documents / "install" / "thesis.txt").exists(), (
        "removal followed the symlinked game folder and deleted the symlink target"
    )
    assert result.trees_removed == [], result.trees_removed
    assert result.warnings, "following a symlink must at least be reported"


def test_a_library_behind_a_symlink_from_the_start_is_still_removed(tmp_path):
    """The legitimate case: ``~/Games`` was a link to another disk when the game was added.
    The record holds the resolved folder, so Uninstall still deletes what Nadare made."""
    disk = tmp_path / "disk" / "Games"
    disk.mkdir(parents=True)
    link = tmp_path / "Games"
    os.symlink(disk, link, target_is_directory=True)
    install = link / "Nadare" / "My Game" / "install"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "g", backend="wine", title="My Game", install_dir=str(install),
        owned_dirs=[str(install.parent)], owned_trees=["install_dir"],
        owned_paths={"install_dir": str(install.resolve())},
    )
    cfg.save()
    result = removal.remove_game(
        cfg, "g", delete_trees=["install_dir"], applications_dir=tmp_path / "apps"
    )
    print("warnings:", result.warnings, "removed:", result.trees_removed)
    assert not (disk / "Nadare" / "My Game" / "install").exists()


def test_an_old_record_without_paths_is_not_deleted_through_a_link(tmp_path):
    """Profiles from before 0.1.0b13 recorded no paths: a link anywhere on the way means the
    folder is not provably the one Nadare made."""
    lib = tmp_path / "Games"
    game = lib / "Nadare" / "My Game"
    (game / "install").mkdir(parents=True)
    documents = tmp_path / "Documents"
    (documents / "install").mkdir(parents=True)
    (documents / "install" / "thesis.txt").write_text("mine")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "old", backend="wine", title="My Game", install_dir=str(game / "install"),
        owned_trees=["install_dir"],
    )
    cfg.save()
    os.rename(game, tmp_path / "moved-away")
    os.symlink(documents, game, target_is_directory=True)
    result = removal.remove_game(
        cfg, "old", delete_trees=["install_dir"], applications_dir=tmp_path / "apps"
    )
    print("warnings:", result.warnings, "removed:", result.trees_removed)
    assert (documents / "install" / "thesis.txt").exists()
