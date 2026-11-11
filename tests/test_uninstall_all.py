# SPDX-License-Identifier: GPL-3.0-or-later
"""Uninstall everything Nadare put on this computer (#950), group by group.

Every game goes through the same per-game removal as Remove (shortcuts, saved login, only folders
Nadare made, mounted drives and symlinks left alone). Nadare's own folders are deleted with the
same mount-aware helper and never take a game folder or backup the user chose to keep. Without
``--yes`` the command only shows what it would do.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare import app
from nadare.core import paths, removal, uninstall_all
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path):
    """tests/conftest.py points HOME and the XDG folders at tmp_path."""
    for fn in (paths.data_dir, paths.config_dir, paths.state_dir, paths.cache_dir):
        fn().mkdir(parents=True, exist_ok=True)
    (paths.cache_dir() / "covers" / "c.webp").parent.mkdir(parents=True)
    (paths.cache_dir() / "covers" / "c.webp").write_bytes(b"img")
    (paths.state_dir() / "logs").mkdir()
    runner = paths.runners_dir() / "dwproton-11.0-15"
    runner.mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    # A game Nadare installed inside its own data folder, and one in the user's Games folder.
    inside = paths.profiles_dir() / "pgr" / "game"
    inside.mkdir(parents=True)
    (inside / "pgr.exe").write_text("MZ")
    outside = tmp_path / "Games" / "Nadare" / "NIKKE"
    outside.mkdir(parents=True)
    (outside / "nikke.exe").write_text("MZ")
    # A game the user brought: Nadare never made this folder.
    theirs = tmp_path / "MyGames" / "Thing"
    theirs.mkdir(parents=True)
    (theirs / "thing.exe").write_text("MZ")
    cfg = Config.load(paths.config_file())
    cfg.add_profile("pgr", backend="direct-pgr", title="PGR", install_dir=str(inside),
                    owned_trees=["install_dir"])
    cfg.add_profile("nikke", backend="launcher-nikke", title="NIKKE", prefix=str(outside),
                    owned_trees=["prefix"])
    cfg.add_profile("thing", backend="wine", title="Thing", install_dir=str(theirs))
    cfg.save()
    return cfg, tmp_path, inside, outside, theirs, runner


def _run(cfg, tmp, **kwargs):
    return uninstall_all.run(cfg, applications_dir=tmp / "apps", desktop_dir=tmp / "desk", **kwargs)


# ---- the plan ------------------------------------------------------------------------------
def test_the_plan_lists_every_group_and_deletes_nothing(env):
    cfg, tmp, inside, outside, theirs, runner = env
    groups = {g.key: g for g in uninstall_all.plan(cfg)}
    assert set(groups) == {"nadare", "runtimes", "games", "backups"}
    assert groups["nadare"].default is True and groups["runtimes"].default is True
    assert groups["games"].default is False and groups["backups"].default is False
    assert inside in groups["games"].paths and outside in groups["games"].paths
    assert theirs not in groups["games"].paths  # Nadare did not make it
    assert paths.config_dir() in groups["nadare"].paths
    assert runner in groups["runtimes"].paths or paths.runners_dir() in groups["runtimes"].paths
    assert inside.is_dir() and outside.is_dir() and runner.is_dir()
    assert paths.config_file().is_file()


# ---- the default run -------------------------------------------------------------------------
def test_the_default_keeps_every_game_and_removes_nadare(env):
    cfg, tmp, inside, outside, theirs, runner = env
    report = _run(cfg, tmp)
    assert not report.refused
    assert (inside / "pgr.exe").is_file()  # a game inside Nadare's data folder is kept
    assert (outside / "nikke.exe").is_file()
    assert (theirs / "thing.exe").is_file()
    assert not paths.config_dir().exists()
    assert not paths.state_dir().exists() and not paths.cache_dir().exists()
    assert not runner.exists()
    assert sorted(report.profiles_removed) == ["nikke", "pgr", "thing"]


def test_runtimes_can_be_kept(env):
    cfg, tmp, inside, outside, theirs, runner = env
    _run(cfg, tmp, runtimes=False)
    assert (runner / "proton").is_file()


def test_with_games_removes_only_folders_nadare_made(env):
    cfg, tmp, inside, outside, theirs, runner = env
    report = _run(cfg, tmp, games=True)
    assert not inside.exists() and not outside.exists()
    assert (theirs / "thing.exe").is_file()
    assert not paths.data_dir().exists()
    assert not report.refused


# ---- what must never be deleted ----------------------------------------------------------------
def test_a_mounted_drive_inside_nadares_folder_is_left_alone(env, monkeypatch):
    cfg, tmp, inside, outside, theirs, runner = env
    mount = paths.data_dir() / "mnt-library"
    mount.mkdir()
    (mount / "sentinel.dat").write_text("other disk")
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        removal.os.path,
        "ismount",
        lambda p: Path(os.fspath(p)) == mount.resolve() or real_ismount(p),
    )
    report = _run(cfg, tmp, games=True)
    assert (mount / "sentinel.dat").read_text() == "other disk"
    assert any(str(mount) in w for w in report.warnings)


def test_a_symlink_in_nadares_folder_is_not_followed(env):
    cfg, tmp, inside, outside, theirs, runner = env
    precious = tmp / "precious"
    precious.mkdir()
    (precious / "photo.jpg").write_bytes(b"jpg")
    (paths.data_dir() / "link").symlink_to(precious, target_is_directory=True)
    _run(cfg, tmp, games=True)
    assert (precious / "photo.jpg").read_bytes() == b"jpg"


def test_a_recorded_backup_is_kept_unless_chosen(env):
    cfg, tmp, inside, outside, theirs, runner = env
    game_dir = tmp / "Games" / "Nadare" / "NIKKE2"
    backup = game_dir / "prefix.broken-20260102-030405"
    backup.mkdir(parents=True)
    (backup / "old.dll").write_text("x")
    cfg.profiles["nikke"].update(prefix_backups=[str(backup)], owned_dirs=[str(game_dir)])
    cfg.save()
    _run(cfg, tmp)
    assert (backup / "old.dll").is_file()


def test_a_running_game_stops_everything_before_anything_is_deleted(env, monkeypatch):
    cfg, tmp, inside, outside, theirs, runner = env
    monkeypatch.setattr(
        removal, "_running_problem",
        lambda config, name: "NIKKE is running; close it first" if name == "nikke" else None,
    )
    report = _run(cfg, tmp, games=True)
    assert report.refused
    assert any("running" in w for w in report.warnings)
    assert paths.config_file().is_file() and inside.is_dir() and runner.is_dir()
    assert set(Config.load(paths.config_file()).profiles) == {"pgr", "nikke", "thing"}


# ---- the command -----------------------------------------------------------------------------
def test_the_command_without_yes_only_shows_the_plan(env, capsys):
    cfg, tmp, inside, outside, theirs, runner = env
    code = app.run_cli(["-c", str(paths.config_file()), "uninstall-all"])
    out = capsys.readouterr().out
    assert code == 0
    assert "--yes" in out and str(paths.config_dir()) in out
    assert paths.config_file().is_file() and runner.is_dir()


def test_the_command_with_yes_uninstalls(env, capsys, monkeypatch):
    cfg, tmp, inside, outside, theirs, runner = env
    monkeypatch.setattr(uninstall_all, "_applications_dir", lambda: tmp / "apps")
    monkeypatch.setattr(uninstall_all, "_desktop_dir", lambda: tmp / "desk")
    code = app.run_cli(["-c", str(paths.config_file()), "uninstall-all", "--yes"])
    assert code == 0
    assert not paths.config_dir().exists()
    assert (inside / "pgr.exe").is_file()


def test_with_games_a_folder_nadare_did_not_make_is_kept_even_inside_its_data(env):
    cfg, tmp, inside, outside, theirs, runner = env
    mine = paths.data_dir() / "my-own-game"
    mine.mkdir()
    (mine / "save.dat").write_text("mine")
    cfg.add_profile("own", backend="wine", title="Own", install_dir=str(mine))
    cfg.save()
    _run(cfg, tmp, games=True)
    assert (mine / "save.dat").read_text() == "mine"


def test_an_install_in_progress_stops_everything_before_anything_is_deleted(env):
    from nadare.core import process

    cfg, tmp, inside, outside, theirs, runner = env
    lock = process.OperationLock("nikke")
    lock.acquire()
    try:
        report = _run(cfg, tmp, games=True)
    finally:
        lock.release()
    assert report.refused
    assert set(Config.load(paths.config_file()).profiles) == {"pgr", "nikke", "thing"}
    assert inside.is_dir() and runner.is_dir()


# ---- review 2026-11-09: a linked Nadare folder and a shared game folder -------------------------
def test_a_nadare_folder_that_is_a_link_is_never_walked(env):
    """~/.local/share/nadare linked to a big disk: uninstall-all must not empty the link target."""
    import shutil

    cfg, tmp, inside, outside, theirs, runner = env
    target = tmp / "bigdisk"
    target.mkdir()
    (target / "family.jpg").write_bytes(b"jpg")  # the user's own file next to Nadare's data
    data = paths.data_dir()
    for child in list(data.iterdir()):
        shutil.move(str(child), str(target / child.name))
    data.rmdir()
    data.symlink_to(target, target_is_directory=True)
    report = _run(cfg, tmp)  # the default keeps games, so the game inside is a keep tree
    assert (target / "family.jpg").read_bytes() == b"jpg"
    assert data.is_symlink()
    assert any(str(data) in w and "link" in w for w in report.warnings)


def test_a_folder_two_games_share_is_not_deleted_when_the_plan_did_not_list_it(env):
    cfg, tmp, inside, outside, theirs, runner = env
    shared = tmp / "Games" / "Shared"
    shared.mkdir(parents=True)
    (shared / "keep.dat").write_text("both use it")
    cfg.add_profile("a", backend="wine", title="A", install_dir=str(shared),
                    owned_trees=["install_dir"])
    cfg.add_profile("b", backend="wine", title="B", prefix=str(shared), owned_trees=["prefix"])
    cfg.save()
    groups = {g.key: g for g in uninstall_all.plan(cfg)}
    assert shared not in groups["games"].paths
    _run(cfg, tmp, games=True)
    assert (shared / "keep.dat").read_text() == "both use it"
