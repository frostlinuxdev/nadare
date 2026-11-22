# SPDX-License-Identifier: GPL-3.0-or-later
"""A folder recorded by Nadare is proven by device, inode AND a token marker (b25 public CI).

ext4 gives a deleted folder's inode number to the next folder made. b25 proved "this is the
folder Nadare moved/copied/set aside" by device and inode alone, so a different folder put at
the same path could pass: Remove game then offered to delete it, and the next start could delete
it or adopt it. Every test here makes the reuse certain with the ``inode_reuse`` fixture, so it
fails the same way on tmpfs, btrfs and ext4.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from nadare.core import folderproof, importmove, prefixmove, relink, relocate, removal
from nadare.core.config import Config
from tests.inode_reuse import inode_reuse  # noqa: F401 - pytest fixture

# ---- the module ---------------------------------------------------------------------------


def test_a_stamped_folder_matches_its_record(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    proof = folderproof.stamp(folder, "moved")
    assert folderproof.matches(folder, "moved", proof.record())
    assert (folder / ".nadare-proof-moved").read_text().strip() == proof.token


def test_another_folder_with_the_reused_inode_does_not_match(tmp_path, inode_reuse):  # noqa: F811
    folder = tmp_path / "f"
    folder.mkdir()
    proof = folderproof.stamp(folder, "moved")
    inode_reuse.reuse(folder)
    shutil.rmtree(folder)
    folder.mkdir()
    st = os.lstat(folder)
    assert [st.st_dev, st.st_ino] == [proof.dev, proof.ino]  # the reuse really happens
    assert not folderproof.matches(folder, "moved", proof.record())


def test_a_copy_with_the_marker_does_not_match(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    proof = folderproof.stamp(folder, "original")
    backup = tmp_path / "backup"
    shutil.copytree(folder, backup)
    shutil.rmtree(folder)
    backup.rename(folder)  # a backup put back: right token, other inode
    assert not folderproof.matches(folder, "original", proof.record())


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"id": [1, 2]},
        {"id": [1, 2], "token": "short"},
        {"id": [1, True], "token": "0" * 32},
        {"id": "1,2", "token": "0" * 32},
    ],
)
def test_odd_or_legacy_records_never_match(tmp_path, record):
    folder = tmp_path / "f"
    folder.mkdir()
    folderproof.stamp(folder, "moved")
    assert not folderproof.matches(folder, "moved", record)


def test_a_wrong_token_or_role_does_not_match(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    proof = folderproof.stamp(folder, "moved")
    assert not folderproof.matches(folder, "original", proof.record())
    other = {**proof.record(), "token": folderproof.new_token()}
    assert not folderproof.matches(folder, "moved", other)


def test_a_linked_folder_or_marker_does_not_match(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    proof = folderproof.stamp(real, "moved")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert not folderproof.matches(link, "moved", proof.record())
    marker = real / ".nadare-proof-moved"
    (tmp_path / "token").write_text(proof.token)
    marker.unlink()
    marker.symlink_to(tmp_path / "token")
    assert not folderproof.matches(real, "moved", proof.record())


def test_stamp_refuses_a_link_and_try_stamp_gives_none(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        folderproof.stamp(link, "moved")
    assert folderproof.try_stamp(link, "moved") is None
    assert folderproof.try_stamp(tmp_path / "missing", "moved") is None
    assert not (real / ".nadare-proof-moved").exists()


def test_restamping_replaces_the_token_and_leaves_no_temp_file(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    first = folderproof.stamp(folder, "move")
    second = folderproof.stamp(folder, "move")
    assert not folderproof.matches(folder, "move", first.record())
    assert folderproof.matches(folder, "move", second.record())
    assert sorted(p.name for p in folder.iterdir()) == [".nadare-proof-move"]


def test_roles_have_their_own_markers(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    a = folderproof.stamp(folder, "moved")
    b = folderproof.stamp(folder, "original")
    assert folderproof.matches(folder, "moved", a.record())
    assert folderproof.matches(folder, "original", b.record())
    with pytest.raises(ValueError):
        folderproof.marker_name("nope")


def test_clear_removes_only_that_marker(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    folderproof.stamp(folder, "moved")
    folderproof.stamp(folder, "move")
    folderproof.clear(folder, "move")
    folderproof.clear(tmp_path / "missing", "move")  # never raises
    assert sorted(p.name for p in folder.iterdir()) == [".nadare-proof-moved"]


def test_the_move_comparison_ignores_every_marker():
    assert folderproof.MARKER_NAMES <= relocate._OWN_FILES


# ---- 1: Remove game's "delete the prefix" for a folder Nadare moved (relink) ----------------


def _prefix(path: Path) -> Path:
    (path / "drive_c" / "windows").mkdir(parents=True)
    (path / "system.reg").write_text("reg")
    return path


def _aether(tmp_path):
    from tests.test_1366_leftovers import _config

    library = tmp_path / "lib"
    home = library / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()
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
    return cfg, new


def _prefix_safe(cfg) -> bool:
    trees = {tree.key: tree for tree in removal.removable_trees(cfg, "aether")}
    return trees["prefix"].safe


def test_1_the_moved_prefix_is_still_removable(tmp_path):
    cfg, new = _aether(tmp_path)
    assert relink.moved_by_nadare(cfg.profiles["aether"], new)
    assert _prefix_safe(cfg)


def test_1_another_folder_with_the_reused_inode_is_kept(tmp_path, inode_reuse):  # noqa: F811
    cfg, new = _aether(tmp_path)
    inode_reuse.reuse(new)
    shutil.rmtree(new)
    _prefix(new)
    assert not relink.moved_by_nadare(cfg.profiles["aether"], new)
    assert not _prefix_safe(cfg)


def test_1_a_b25_record_without_a_token_is_not_proof(tmp_path):
    cfg, new = _aether(tmp_path)
    proof = cfg.profiles["aether"][relink.MOVED_BY_NADARE]
    for entry in proof.values():
        entry.pop("token")
    assert not relink.moved_by_nadare(cfg.profiles["aether"], new)
    assert not _prefix_safe(cfg)


# ---- 2: a Move's original, deleted by the next start (relocate pending originals) ---------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for key, leaf in (
        ("HOME", "home"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    return Config.load(tmp_path / "config.json")


def _pending(cfg, tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    changes = relocate.with_pending_original({"prefix": str(new)}, {}, old, new)
    cfg.add_profile("bd2", backend="direct-bd2", title="BD2", **changes)
    return old, new


def test_2_the_copied_original_is_still_finished(cfg, tmp_path):
    old, new = _pending(cfg, tmp_path)
    assert relocate.pending_originals(cfg) == [("bd2", old, new)]
    assert relocate.remove_original(old, new).removed  # its marker is not a difference


def test_2_a_folder_with_the_reused_inode_is_never_deleted(cfg, tmp_path, inode_reuse):  # noqa: F811
    old, new = _pending(cfg, tmp_path)
    inode_reuse.reuse(old)
    shutil.rmtree(old)
    _prefix(old)  # the player's own folder at that path
    assert relocate.pending_originals(cfg) == []


def test_2_a_b25_entry_without_a_token_is_never_acted_on(cfg, tmp_path):
    old, _new = _pending(cfg, tmp_path)
    profile = cfg.profiles["bd2"]
    profile[relocate.PENDING_ORIGINALS] = [e[:4] for e in profile[relocate.PENDING_ORIGINALS]]
    assert relocate.pending_originals(cfg) == []
    assert relocate.prune_pending_originals(profile)
    assert relocate.PENDING_ORIGINALS not in profile
    assert old.is_dir()


def test_2_an_original_that_cannot_take_a_marker_is_never_acted_on(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(folderproof, "stamp", _no_stamp)
    old, _new = _pending(cfg, tmp_path)
    assert relocate.pending_originals(cfg) == []


def _no_stamp(*_args, **_kwargs):
    raise OSError(30, "read-only file system")


# ---- 3: a Move killed mid-way (relocate move journal) -------------------------------------


class _Killed(BaseException):
    pass


def _killed_copy(tmp_path, monkeypatch):
    """A cross-disk Move killed while copying: the partial copy and the record stay."""
    src = _prefix(tmp_path / "src" / "prefix")
    dst = tmp_path / "dst" / "prefix"
    dst.parent.mkdir()
    profile: dict = {"prefix": str(src)}
    relocate.begin_move(profile, src, dst)
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    real_copy = relocate._copy

    def dying(src_, partial, *args):
        real_copy(src_, partial, *args)
        raise _Killed

    monkeypatch.setattr(relocate, "_copy", dying)
    monkeypatch.setattr(relocate, "_remove_partial", lambda p: None)  # killed: no cleanup
    with pytest.raises(_Killed):
        relocate.move_tree(src, dst, journal=profile[relocate.MOVE_IN_PROGRESS])
    monkeypatch.undo()
    partial = dst.with_name(dst.name + relocate.PARTIAL_SUFFIX)
    assert partial.is_dir()
    return profile, src, dst, partial


def test_3_the_next_start_removes_its_own_partial_copy(tmp_path, monkeypatch):
    profile, src, _dst, partial = _killed_copy(tmp_path, monkeypatch)
    notes = relocate.finish_interrupted_moves({"g": profile})
    assert not partial.exists(), notes
    assert (src / "system.reg").read_text() == "reg"


def test_3_a_folder_with_the_reused_inode_at_the_partial_path_is_kept(
    tmp_path,
    monkeypatch,
    inode_reuse,  # noqa: F811
):
    profile, _src, _dst, partial = _killed_copy(tmp_path, monkeypatch)
    inode_reuse.reuse(partial)
    shutil.rmtree(partial)
    partial.mkdir()
    (partial / "mine.txt").write_text("the player's")
    relocate.finish_interrupted_moves({"g": profile})
    assert (partial / "mine.txt").read_text() == "the player's"


def _killed_after_rename(tmp_path):
    """A same-disk Move renamed the folder, then died before the profile was saved."""
    src = _prefix(tmp_path / "src" / "prefix")
    dst = tmp_path / "dst" / "prefix"
    dst.parent.mkdir()
    profile: dict = {"prefix": str(src)}
    relocate.begin_move(profile, src, dst)
    relocate.move_tree(src, dst, journal=profile[relocate.MOVE_IN_PROGRESS])
    return profile, src, dst


def test_3_a_renamed_move_is_finished(tmp_path):
    profile, _src, dst = _killed_after_rename(tmp_path)
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(dst)


def test_3_a_folder_with_the_reused_inode_at_the_destination_is_not_adopted(
    tmp_path,
    inode_reuse,  # noqa: F811
):
    profile, src, dst = _killed_after_rename(tmp_path)
    inode_reuse.reuse(dst)
    shutil.rmtree(dst)
    dst.mkdir()
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(src)
    assert relocate.MOVE_IN_PROGRESS in profile  # kept for the player to settle


def test_3_a_b25_record_without_a_token_is_not_adopted(tmp_path):
    profile, src, _dst = _killed_after_rename(tmp_path)
    profile[relocate.MOVE_IN_PROGRESS].pop("token")
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(src)


# ---- 4: an import-move killed mid-way (importmove journal) -----------------------------


def _import_setup(tmp_path, monkeypatch):
    from tests.test_1400_importmove_copy_recovery import _killed_after_first_copy, _setup

    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    cfg_, library, install, prefix = _setup(tmp_path)
    move, _ = _killed_after_first_copy(cfg_, library, monkeypatch)
    return cfg_, move, install


def test_4_its_own_copy_is_still_removed(tmp_path, monkeypatch):
    cfg_, move, install = _import_setup(tmp_path, monkeypatch)
    copy = move.trees[0][2]
    importmove.finish_interrupted(Config.load(cfg_.path))
    assert not copy.exists()
    assert install.is_dir()


def test_4_a_folder_with_the_reused_inode_is_never_deleted(tmp_path, monkeypatch, inode_reuse):  # noqa: F811
    cfg_, move, install = _import_setup(tmp_path, monkeypatch)
    inode_reuse.reuse(move.trees[0][2])
    copy = move.trees[0][2]
    shutil.rmtree(copy)
    copy.mkdir()
    (copy / "mine.txt").write_text("the player's")
    importmove.finish_interrupted(Config.load(cfg_.path))
    assert (copy / "mine.txt").read_text() == "the player's"
    assert install.is_dir()


def test_4_a_b25_journal_without_tokens_deletes_nothing(tmp_path, monkeypatch):
    cfg_, move, _install = _import_setup(tmp_path, monkeypatch)
    data = json.loads(move.journal.read_text())
    for tree in data["trees"]:
        tree.pop("token", None)
    move.journal.write_text(json.dumps(data))
    copy = move.trees[0][2]
    notes = importmove.finish_interrupted(Config.load(cfg_.path))
    assert copy.is_dir() and move.journal.exists(), notes


def test_4_a_renamed_tree_with_the_reused_inode_is_not_adopted(tmp_path, monkeypatch, inode_reuse):  # noqa: F811
    """Same disk: every tree renamed, then killed before the profile was saved."""
    from tests.test_1400_importmove_copy_recovery import _setup

    cfg_, library, install, prefix = _setup(tmp_path)
    move = importmove.plan(cfg_, "g", library)
    monkeypatch.setattr(importmove, "_clear_journal", lambda m: None)
    importmove.run(move)
    monkeypatch.undo()
    for _key, _src, dst in move.trees:
        inode_reuse.reuse(dst)
        shutil.rmtree(dst)
        dst.mkdir()
        (dst / "mine.txt").write_text("the player's")
    again = Config.load(cfg_.path)
    importmove.finish_interrupted(again)
    assert again.profiles["g"]["install_dir"] == str(install)
    assert again.profiles["g"]["prefix"] == str(prefix)
    assert "owned_trees" not in again.profiles["g"]


# ---- 5: a prefix rebuild killed mid-way (prefixmove) ------------------------------------


def _killed_rebuild(tmp_path, monkeypatch):
    from tests import test_1490_rebuild_intent as t

    for key, leaf in (
        ("HOME", "home"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    cfg_, prefix, game = t._profile(tmp_path, "arknights-yostar")
    t._killed_rebuild(cfg_, "arknights-yostar", "ensure")
    return cfg_, prefix, t._backups(prefix)[0]


def test_5_the_backup_is_still_put_back(tmp_path, monkeypatch):
    cfg_, prefix, _backup = _killed_rebuild(tmp_path, monkeypatch)
    again = Config.load(cfg_.path)
    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes and "put back" in notes[0], notes
    assert (prefix / "system.reg").read_text() == "old registry"


def test_5_a_folder_with_the_reused_inode_is_never_put_back(tmp_path, monkeypatch, inode_reuse):  # noqa: F811
    cfg_, prefix, backup = _killed_rebuild(tmp_path, monkeypatch)
    inode_reuse.reuse(backup)
    shutil.rmtree(backup)
    backup.mkdir()
    (backup / "other.txt").write_text("not the old prefix")
    again = Config.load(cfg_.path)
    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes and "kept" in notes[0], notes
    assert (backup / "other.txt").exists()
    assert again.profiles["synthetic"].get(prefixmove.REBUILD_KEY)


def test_5_a_b25_record_without_a_token_moves_nothing(tmp_path, monkeypatch):
    cfg_, prefix, backup = _killed_rebuild(tmp_path, monkeypatch)
    again = Config.load(cfg_.path)
    again.profiles["synthetic"][prefixmove.REBUILD_KEY].pop("token")
    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes and "kept" in notes[0], notes
    assert backup.is_dir()


def test_4_a_copy_swapped_after_the_journal_was_read_is_never_deleted(
    tmp_path, monkeypatch, inode_reuse  # noqa: F811
):
    """The removal checks the proof again: a swap between reading the journal and deleting."""
    cfg_, move, install = _import_setup(tmp_path, monkeypatch)
    copy = move.trees[0][2]
    real = importmove._recover_move

    def then_swapped(*args, **kwargs):
        recovered = real(*args, **kwargs)
        inode_reuse.reuse(copy)
        shutil.rmtree(copy)
        copy.mkdir()
        (copy / "mine.txt").write_text("the player's")
        return recovered

    monkeypatch.setattr(importmove, "_recover_move", then_swapped)
    importmove.finish_interrupted(Config.load(cfg_.path))
    assert (copy / "mine.txt").read_text() == "the player's"


def test_5_a_record_whose_prefix_was_replaced_is_kept(tmp_path, monkeypatch, inode_reuse):  # noqa: F811
    """Killed before the rename, then the prefix was replaced: the record is not just dropped."""
    for key, leaf in (("HOME", "home"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                      ("XDG_CONFIG_HOME", "config")):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    from tests import test_1490_rebuild_intent as t

    cfg_, prefix, _game = t._profile(tmp_path, "arknights-yostar")
    proof = folderproof.stamp(prefix, "rebuild")
    backup = prefixmove.backup_path(prefix)
    profile = cfg_.profiles["synthetic"]
    profile["prefix_backups"] = [str(backup.resolve())]
    profile[prefixmove.REBUILD_KEY] = {
        "prefix": str(prefix), "backup": str(backup.resolve()), **proof.record(), "keep": [],
    }
    inode_reuse.reuse(prefix)
    shutil.rmtree(prefix)
    _prefix(prefix)
    notes = prefixmove.finish_interrupted_rebuilds(cfg_)
    assert notes and "kept" in notes[0], notes
    assert profile.get(prefixmove.REBUILD_KEY)
    assert profile.get("prefix_backups")


# ---- a marker left in a game folder is Nadare's own, like its manifest ----------------------


def _folder_with_manifest_and_marker(tmp_path):
    from nadare.core import libraries

    folder = tmp_path / "lib" / "Nadare" / "Game"
    folder.mkdir(parents=True)
    (folder / libraries.MANIFEST_NAME).write_text("{}")
    folderproof.stamp(folder, "move")  # a whole-game Move leaves it there
    return folder


def test_a_lone_manifest_and_marker_are_tidied_after_remove_game(tmp_path):
    folder = _folder_with_manifest_and_marker(tmp_path)
    removal._drop_lone_manifest(folder)
    assert list(folder.iterdir()) == []


def test_a_folder_with_only_manifest_and_marker_is_free_to_use_again(tmp_path):
    folder = _folder_with_manifest_and_marker(tmp_path)
    assert relink._stale_folder(folder, {})
    assert relink.clear_stale_folder(folder, {})
    assert not folder.exists()


def test_a_marker_is_not_a_game_file(tmp_path):
    from nadare.gui.settings_parts.prefix_tools import has_game_files

    folder = _folder_with_manifest_and_marker(tmp_path)
    assert not has_game_files(folder)
    (folder / "game.exe").write_bytes(b"MZ")
    assert has_game_files(folder)


def test_a_marker_does_not_stop_a_whole_game_move(tmp_path):
    home = tmp_path / "profiles" / "g"
    (home / "prefix").mkdir(parents=True)
    profile = {"prefix": str(home / "prefix")}
    assert relink._profile_dir_moves_whole(profile, home, exists=True)
    folderproof.stamp(home, "move")
    assert relink._profile_dir_moves_whole(profile, home, exists=True)
