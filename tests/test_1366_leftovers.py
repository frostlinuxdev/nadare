# SPDX-License-Identifier: GPL-3.0-or-later
"""#1366 leftovers: which storage findings still reproduce on ``next``.

One test per item from the issue and its follow-up rounds. A test that passes is a regression
guard for behaviour that is now correct; a strict ``xfail`` is the evidence that the item still
reproduces. Every test builds its own folders under ``tmp_path`` and drives the real
``relocate``/``relink``/``libraries``/``removal`` code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import gamefolders, libraries, paths, relink, relocate, removal
from nadare.core.config import Config
from nadare.core.events import Cancel, Cancelled


def _prefix(root: Path) -> Path:
    (root / "drive_c").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def _config(tmp_path: Path, profiles: dict | None = None, roots: tuple[Path, ...] = ()) -> Config:
    data = Config._normalise(
        {
            "profiles": dict(profiles or {}),
            "libraries": [{"path": str(root)} for root in roots],
        }
    )
    return Config(path=tmp_path / "config.json", data=data)


# ---- A8: a kill mid-copy leaves <dst>.nadare-partial and blocks the name --------------------
def test_a8_a_kill_mid_copy_no_longer_blocks_the_same_name(tmp_path):
    """The journal's partial identity lets the next start remove its own unfinished copy."""
    library = tmp_path / "lib"
    home = library / "Nadare" / "Game"
    _prefix(home / "prefix")
    dst = tmp_path / "lib2" / "Nadare" / "Game"
    profile = {
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["prefix"],
    }
    cfg = _config(tmp_path, {"game": profile}, roots=(library,))
    current = cfg.profiles["game"]
    relocate.begin_move(current, home / "prefix", dst)
    cfg.save()
    partial = dst.with_name(dst.name + relocate.PARTIAL_SUFFIX)
    _prefix(partial)  # the copy the kill cut short
    relocate.record_partial(current[relocate.MOVE_IN_PROGRESS], partial)
    cfg.save()  # the checkpoint move_tree writes before it starts copying
    # The process is killed here: no cleanup, no profile save after the move.

    libraries.backfill(cfg)  # the next start

    assert not partial.exists()
    assert relocate.MOVE_IN_PROGRESS not in cfg.profiles["game"]
    moved = relocate.move_tree(home / "prefix", dst)  # a later Move to the very same name
    assert moved.renamed
    assert dst.is_dir() and not (home / "prefix").exists()


# ---- C2: a pre-existing install in a library must not invent profiles/<name> ----------------
def test_c2_the_game_folder_shown_is_a_real_one_and_nothing_is_outside_it(tmp_path):
    """owned_trees=["prefix"] plus an unowned install must not fall back to profiles/<name>."""
    library = tmp_path / "lib"
    home = library / "Nadare" / "Game"
    _prefix(home / "prefix")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ")
    profile = {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_trees": ["prefix"],
        "owned_paths": {"prefix": str((home / "prefix").resolve())},
    }
    cfg = _config(tmp_path, {"game": profile}, roots=(library,))

    invented = paths.profiles_dir() / "game"
    assert libraries._game_folder(profile, "game", None) != invented

    place = gamefolders.placement(cfg, "game")
    assert place is not None
    assert place.folder is not None and place.folder.is_dir()  # never a folder that is not there
    assert place.stray == {}  # neither real folder is called "outside the game folder"


# ---- A3: a prefix-only Move by Nadare leaves an undeletable prefix --------------------------
def test_a3_a_prefix_moved_by_nadare_alone_can_be_removed(tmp_path):
    library = tmp_path / "lib"
    home = library / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()  # the game folder then goes missing: only a prefix-only Move
    profile = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str((home / "install").resolve()),
            "prefix": str((home / "prefix").resolve()),
        },
    }
    cfg = _config(tmp_path, {"aether": profile}, roots=(library,))
    (home / "install").rmdir()
    new = tmp_path / "lib2" / "Aether Gazer prefix"
    new.parent.mkdir()
    relocate.move_tree(home / "prefix", new)
    plan = relink.plan_relink(cfg.profiles["aether"], new, made_by_nadare=True)
    relink.apply(cfg.profiles["aether"], plan)

    trees = {tree.key: tree for tree in removal.removable_trees(cfg, "aether")}
    assert "prefix" in trees
    assert trees["prefix"].safe, trees["prefix"].reason



def test_a3_a_hand_edited_owned_path_outside_is_still_kept(tmp_path):
    """#1229: an owned_paths entry written by hand (no move proof) keeps the folder."""
    library = tmp_path / "lib"
    home = library / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()  # the game folder then goes missing: only a prefix-only Move
    profile = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str((home / "install").resolve()),
            "prefix": str((home / "prefix").resolve()),
        },
    }
    cfg = _config(tmp_path, {"aether": profile}, roots=(library,))
    (home / "install").rmdir()
    new = tmp_path / "lib2" / "Aether Gazer prefix"
    new.parent.mkdir()
    relocate.move_tree(home / "prefix", new)
    plan = relink.plan_relink(cfg.profiles["aether"], new, made_by_nadare=True)
    relink.apply(cfg.profiles["aether"], plan)

    cfg.profiles["aether"].pop(relink.MOVED_BY_NADARE)
    trees = {tree.key: tree for tree in removal.removable_trees(cfg, "aether")}
    assert not trees["prefix"].safe

def test_a3_a_replaced_folder_at_the_same_path_is_kept(tmp_path):
    """Same path, another folder (new inode): not the one Nadare moved."""
    library = tmp_path / "lib"
    home = library / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()  # the game folder then goes missing: only a prefix-only Move
    profile = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str((home / "install").resolve()),
            "prefix": str((home / "prefix").resolve()),
        },
    }
    cfg = _config(tmp_path, {"aether": profile}, roots=(library,))
    (home / "install").rmdir()
    new = tmp_path / "lib2" / "Aether Gazer prefix"
    new.parent.mkdir()
    relocate.move_tree(home / "prefix", new)
    plan = relink.plan_relink(cfg.profiles["aether"], new, made_by_nadare=True)
    relink.apply(cfg.profiles["aether"], plan)

    import shutil

    shutil.rmtree(new)
    _prefix(new)
    trees = {tree.key: tree for tree in removal.removable_trees(cfg, "aether")}
    assert not trees["prefix"].safe

def test_a3_a_hand_relink_drops_the_move_proof(tmp_path):
    """After "I moved it myself" the proof is gone and the folder is kept."""
    library = tmp_path / "lib"
    home = library / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()  # the game folder then goes missing: only a prefix-only Move
    profile = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str((home / "install").resolve()),
            "prefix": str((home / "prefix").resolve()),
        },
    }
    cfg = _config(tmp_path, {"aether": profile}, roots=(library,))
    (home / "install").rmdir()
    new = tmp_path / "lib2" / "Aether Gazer prefix"
    new.parent.mkdir()
    relocate.move_tree(home / "prefix", new)
    plan = relink.plan_relink(cfg.profiles["aether"], new, made_by_nadare=True)
    relink.apply(cfg.profiles["aether"], plan)

    other = tmp_path / "lib3" / "pfx"
    other.parent.mkdir()
    relocate.move_tree(new, other)
    plan = relink.plan_relink(cfg.profiles["aether"], other)
    relink.apply(cfg.profiles["aether"], plan)
    assert "prefix" not in cfg.profiles["aether"].get(relink.MOVED_BY_NADARE, {})
    trees = {tree.key: tree for tree in removal.removable_trees(cfg, "aether")}
    assert "prefix" not in trees or not trees["prefix"].safe

# ---- B-F2: a same-disk Move killed before the save leaves a stale profile -------------------
def test_bf2_a_same_disk_move_whose_save_was_lost_is_finished_at_start(tmp_path):
    library = tmp_path / "lib"
    home = library / "Nadare" / "Game"
    _prefix(home / "prefix")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ")
    profile = {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str((home / "install").resolve()),
            "prefix": str((home / "prefix").resolve()),
        },
    }
    cfg = _config(tmp_path, {"bd2": profile}, roots=(library,))
    cfg.save()
    current = cfg.profiles["bd2"]
    dst = tmp_path / "lib2" / "Nadare" / "Game"
    relocate.begin_move(current, home, dst)
    cfg.save()  # the journal is on disk before anything moves
    assert relocate.move_tree(home, dst, journal=current[relocate.MOVE_IN_PROGRESS]).renamed
    # Killed before the profile save: the game still names the folder that is now gone.
    assert current["prefix"] == str(home / "prefix")

    libraries.backfill(cfg)  # the next start

    assert current["prefix"] == str(dst / "prefix")
    assert current["install_dir"] == str(dst / "install")
    assert relocate.MOVE_IN_PROGRESS not in current


# ---- C-F2: an adopted game in profiles/Nadare/<Game> cannot be uninstalled ------------------
def test_cf2_a_game_adopted_from_profiles_nadare_can_be_uninstalled(tmp_path):
    home = paths.profiles_dir() / "Nadare" / "Brown Dust 2"
    _prefix(home / "prefix")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ")
    (home / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "direct-bd2",
                "title": "Brown Dust 2",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    cfg = _config(tmp_path)
    found, _notes = libraries.scan(cfg, libraries.default_root())
    assert len(found) == 1
    name = libraries.adopt(cfg, found[0])

    trees = removal.removable_trees(cfg, name)
    assert {tree.key for tree in trees} == {"install_dir", "prefix"}
    assert all(tree.safe for tree in trees), [tree.reason for tree in trees]


# ---- small: dst.parent made before the free-space check -------------------------------------
def test_a_refused_move_leaves_no_empty_library_folder(tmp_path, monkeypatch):
    src = _prefix(tmp_path / "src")
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    monkeypatch.setattr(relocate.diskspace, "free_bytes", lambda path: 10)
    dst = tmp_path / "lib" / "Nadare" / "Game"
    with pytest.raises(relocate.RelocateError, match="space"):
        relocate.move_tree(src, dst)
    assert src.is_dir()
    assert not (tmp_path / "lib").exists()


# ---- small: an early Cancel still renames on the same disk ----------------------------------
def test_an_early_cancel_does_not_rename_on_the_same_disk(tmp_path):
    src = _prefix(tmp_path / "src")
    dst = tmp_path / "dst"
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        relocate.move_tree(src, dst, cancel=cancel)
    assert src.is_dir()
    assert not dst.exists()


# ---- s21-leftover: an empty profiles/<name> with a stale manifest survives Uninstall --------
def test_s21_uninstall_leaves_no_stale_profiles_folder_behind(tmp_path):
    home = paths.profiles_dir() / "mixed"
    (home / "install").mkdir(parents=True)
    (home / "install" / "game.exe").write_bytes(b"MZ")
    # A manifest from before D1: Find games reads it and skips the folder with a note.
    (home / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "direct-bd2",
                "title": "Mixed",
                "install": "install",
                "made_by_nadare": [],
            }
        )
    )
    prefix = _prefix(tmp_path / "pfx-mixed")
    profile = {
        "backend": "direct-bd2",
        "title": "Mixed",
        "install_dir": str(home / "install"),
        "prefix": str(prefix),
    }
    cfg = _config(tmp_path, {"mixed": profile})

    result = removal.remove_game(
        cfg,
        "mixed",
        delete_trees=["install_dir"],
        applications_dir=tmp_path / "apps",
    )

    assert not result.profile_kept and "mixed" not in cfg.profiles
    assert not home.exists()  # the empty folder and its stale manifest go
    assert libraries.scan(cfg, libraries.default_root()) == ([], [])


def test_s21_tidy_up_keeps_profiles_folder_with_any_other_file(tmp_path):
    """Negative control: the tidy-up only removes an empty folder (plus a lone manifest)."""
    home = paths.profiles_dir() / "mixed"
    (home / "install").mkdir(parents=True)
    (home / "install" / "game.exe").write_bytes(b"MZ")
    # A manifest from before D1: Find games reads it and skips the folder with a note.
    (home / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "direct-bd2",
                "title": "Mixed",
                "install": "install",
                "made_by_nadare": [],
            }
        )
    )
    (home / "notes.txt").write_text("mine")
    prefix = _prefix(tmp_path / "pfx-mixed")
    profile = {
        "backend": "direct-bd2",
        "title": "Mixed",
        "install_dir": str(home / "install"),
        "prefix": str(prefix),
    }
    cfg = _config(tmp_path, {"mixed": profile})

    result = removal.remove_game(
        cfg,
        "mixed",
        delete_trees=["install_dir"],
        applications_dir=tmp_path / "apps",
    )

    assert not result.profile_kept and "mixed" not in cfg.profiles
    assert (home / "notes.txt").read_text() == "mine"
    assert (home / libraries.MANIFEST_NAME).exists()


# ---- C-F2 negative controls: nothing else inside Nadare's folders opens up -----------------
def _adopted_profile(home):
    return {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
    }


def _game_at(home):
    _prefix(home / "prefix")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ")


def test_cf2_a_folder_another_game_uses_is_still_refused(tmp_path):
    home = paths.profiles_dir() / "Nadare" / "Brown Dust 2"
    _game_at(home)
    other = {"backend": "direct-bd2", "title": "Other", "install_dir": str(home / "install")}
    cfg = _config(tmp_path, {"bd2": _adopted_profile(home), "other": other})
    trees = {t.key: t for t in removal.removable_trees(cfg, "bd2")}
    assert not trees["install_dir"].safe


def test_cf2_a_nadare_folder_outside_profiles_is_still_refused(tmp_path):
    """A hand-edited owned_dirs naming <data>/nadare/<x>/Nadare/<Game> must not open it."""
    home = paths.profiles_dir().parent / "runners" / "Nadare" / "Brown Dust 2"
    _game_at(home)
    cfg = _config(tmp_path, {"bd2": _adopted_profile(home)})
    trees = removal.removable_trees(cfg, "bd2")
    assert trees and not any(t.safe for t in trees), [t.reason for t in trees]


def test_cf2_a_linked_game_folder_is_still_refused(tmp_path):
    real = tmp_path / "Documents" / "Brown Dust 2"
    _game_at(real)
    home = paths.profiles_dir() / "Nadare" / "Brown Dust 2"
    home.parent.mkdir(parents=True)
    home.symlink_to(real)
    cfg = _config(tmp_path, {"bd2": _adopted_profile(home)})
    trees = removal.removable_trees(cfg, "bd2")
    assert not any(t.safe for t in trees), [t.reason for t in trees]
    assert (real / "install" / "game.exe").exists()
