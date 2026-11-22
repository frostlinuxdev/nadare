# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage review round 3 (opencode-go Flash, 2026-11-14).

A-1/A-2: Move could put a game inside another game's own profiles/<name>, where that game's
  data deletion later took it.
B-F3/C-F1: a pending-original record deleted any name+size-identical copy at start (a backup).
B-F1: a relative link leading out of the moved folder dangled from the copy; the original went.
C-F3: Uninstall tidied another profile's profiles/<name> (its manifest and empty folder).
C-F4: sync_game still raised on a NUL path outside the round-2 guard.
B-F5: after a prefix-only Move the manifest kept naming the old prefix.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from nadare.core import libraries, paths, relink, relocate, removal
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home


def _prefix(root: Path) -> Path:
    (root / "drive_c" / "windows").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


# ---- A-1 / A-2 ------------------------------------------------------------------------------
def test_a_move_into_another_games_own_folder_is_refused(env):
    cfg, _home = env
    beta = paths.profiles_dir() / "beta"
    _prefix(beta / "prefix")
    cfg.add_profile("beta", backend="direct-bd2", title="Beta")  # names no folder
    cfg.add_profile("alpha", backend="direct-bd2", title="Alpha")
    target = beta / "Nadare" / "Alpha"
    assert relink._inside_other(cfg.profiles, "alpha", target) == "Beta"
    with pytest.raises(relink.RelinkError):
        relink.check_home_target(cfg.profiles["alpha"], beta, name="alpha", profiles=cfg.profiles)


# ---- B-F3 / C-F1 ----------------------------------------------------------------------------
def _pending(cfg: Config, old: Path, new: Path) -> None:
    changes = relocate.with_pending_original({"prefix": str(new)}, {}, old, new)
    cfg.add_profile("bd2", backend="direct-bd2", title="BD2", **changes)


def test_the_folder_the_move_copied_is_finished(env, tmp_path):
    cfg, _home = env
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    _pending(cfg, old, new)
    assert relocate.pending_originals(cfg) == [("bd2", old, new)]


def test_a_backup_made_later_at_that_path_is_never_deleted(env, tmp_path):
    cfg, _home = env
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    _pending(cfg, old, new)
    shutil.rmtree(old)
    shutil.copytree(new, old, symlinks=True)  # the player's backup: same names and sizes
    assert relocate.pending_originals(cfg) == []


def test_a_record_without_the_folders_identity_is_never_acted_on(env, tmp_path):
    cfg, _home = env
    old = _prefix(tmp_path / "Documents" / "bd2-backup")
    new = tmp_path / "b" / "prefix"
    shutil.copytree(old, new, symlinks=True)
    cfg.add_profile(
        "bd2",
        backend="direct-bd2",
        title="BD2",
        prefix=str(new),
        **{relocate.PENDING_ORIGINALS: [[str(old), str(new)]]},  # hand-written or restored
    )
    assert relocate.pending_originals(cfg) == []
    assert relocate.prune_pending_originals(cfg.profiles["bd2"]) is True
    assert relocate.PENDING_ORIGINALS not in cfg.profiles["bd2"]


# ---- B-F1 -----------------------------------------------------------------------------------
def test_a_relative_link_leading_out_of_the_folder_keeps_the_original(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    (tmp_path / "a" / "saves").mkdir()
    (tmp_path / "a" / "saves" / "slot1").write_text("save")
    os.symlink("../../saves", old / "drive_c" / "saves")
    new = tmp_path / "b" / "prefix"
    new.parent.mkdir()
    shutil.copytree(old, new, symlinks=True)
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and old.is_dir()


def test_wine_drive_links_still_allow_the_removal(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    (old / "dosdevices").mkdir()
    os.symlink("../drive_c", old / "dosdevices" / "c:")
    os.symlink("/", old / "dosdevices" / "z:")
    new = tmp_path / "b" / "prefix"
    new.parent.mkdir()
    shutil.copytree(old, new, symlinks=True)
    assert relocate.remove_original(old, new).removed


# ---- C-F3 -----------------------------------------------------------------------------------
def test_uninstall_never_tidies_another_profiles_folder(env):
    _cfg, _home = env
    other = paths.profiles_dir() / "b"
    (other / "prefix").mkdir(parents=True)
    profile = {"backend": "wine", "prefix": str(other / "prefix")}
    assert str(other) not in removal._wrapper_folders(profile, "a")
    assert str(other) in removal._wrapper_folders(profile, "b")


# ---- C-F4 -----------------------------------------------------------------------------------
def test_sync_game_survives_a_nul_in_a_path(env):
    cfg, _home = env
    home = paths.profiles_dir() / "bd2"
    (home / "install").mkdir(parents=True)
    cfg.add_profile(
        "bd2",
        backend="direct-bd2",
        title="BD2",
        install_dir=str(home / "install"),
        prefix=str(home / "prefix") + "\x00evil",
    )
    assert libraries.sync_game(cfg, "bd2") is False


# ---- B-F5 -----------------------------------------------------------------------------------
def test_the_manifest_never_names_a_prefix_moved_out_of_the_folder(env, tmp_path):
    cfg, _home = env
    game = paths.profiles_dir() / "nikke"
    (game / "install").mkdir(parents=True)
    moved = _prefix(tmp_path / "Games" / "NIKKE prefix")
    profile = {
        "backend": "direct-bd2",
        "title": "NIKKE",
        "install_dir": str(game / "install"),
        "prefix": str(moved),
    }
    written = libraries.write_manifest(profile, game)
    data = json.loads(written.read_text())
    assert "prefix" not in data and data["install"] == "install"
    cfg.add_profile("nikke", **profile, owned_trees=["install_dir"])
    libraries.sync_game(cfg, "nikke")
    before = written.stat().st_mtime_ns
    libraries.sync_game(cfg, "nikke")
    assert written.stat().st_mtime_ns == before  # stable: not rewritten on every start
