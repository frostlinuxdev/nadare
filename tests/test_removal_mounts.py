# SPDX-License-Identifier: GPL-3.0-or-later
"""Removal never descends into a mounted drive that sits inside a game's tree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import removal
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    return cfg, tmp_path


@pytest.fixture
def fake_mount(monkeypatch):
    """Make ``os.path.ismount`` report one path as a mount point (tests cannot mount)."""
    real_ismount = os.path.ismount

    def ismount(path):
        return Path(os.fspath(path)) == fake_mount.path or real_ismount(path)

    monkeypatch.setattr(removal.os.path, "ismount", ismount)
    return fake_mount


def test_a_mounted_drive_inside_the_tree_is_left_alone(env, fake_mount):
    cfg, tmp = env
    install = tmp / "Games" / "gone"
    mount = install / "drive_c" / "Games" / "Big"
    mount.mkdir(parents=True)
    (mount / "sentinel.dat").write_text("other disk")
    (install / "game.exe").write_text("x")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    fake_mount.path = mount.resolve()

    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )

    assert (mount / "sentinel.dat").exists(), "data on the mounted drive was deleted"
    assert mount.is_dir()
    assert not (install / "game.exe").exists(), "the rest of the tree was not deleted"
    assert any("separate mounted drive" in w and "left alone" in w for w in result.warnings), (
        result.warnings
    )
    assert any(str(mount) in w for w in result.warnings), result.warnings
    assert result.trees_removed == [], "a partly removed tree was reported as removed"
    assert result.profile_kept and "gone" in cfg.profiles


def test_a_symlink_inside_the_tree_is_not_followed(env):
    cfg, tmp = env
    install = tmp / "Games" / "gone"
    outside = tmp / "precious"
    outside.mkdir()
    (outside / "sentinel.dat").write_text("keep me")
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    (install / "link").symlink_to(outside, target_is_directory=True)
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])

    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )

    assert (outside / "sentinel.dat").exists(), "a symlinked folder was followed"
    assert not install.exists()
    assert result.trees_removed and not result.warnings


def test_a_bind_mount_on_the_same_filesystem_is_left_alone(env, monkeypatch):
    """A bind mount of a folder from the same disk has the same st_dev and is not seen
    by ``os.path.ismount``; /proc/self/mountinfo is the only place that lists it."""
    cfg, tmp = env
    install = tmp / "Games" / "gone"
    bind = install / "savedata"
    bind.mkdir(parents=True)
    (bind / "saves.dat").write_text("the user's saves")
    (install / "game.exe").write_text("x")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    monkeypatch.setattr(
        removal, "_mountinfo_points", lambda: frozenset({str(bind.resolve())})
    )

    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )

    assert (bind / "saves.dat").exists(), "data behind a bind mount was deleted"
    assert not (install / "game.exe").exists()
    assert any("separate mounted drive" in w for w in result.warnings), result.warnings
    assert result.profile_kept


def test_mountinfo_parsing_unescapes_spaces(tmp_path):
    text = (
        "36 35 98:0 /mnt1 /mnt/with\\040space rw - ext3 /dev/root rw\n"
        "37 35 98:0 / /plain rw,noatime shared:1 - ext4 /dev/sda1 rw\n"
        "garbage line\n"
    )
    assert removal._parse_mountinfo(text) == frozenset({"/mnt/with space", "/plain"})
