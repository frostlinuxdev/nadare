# SPDX-License-Identifier: GPL-3.0-or-later
"""Removal reports folders it could not remove instead of dropping them silently."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import paths, removal
from nadare.core.config import Config

requires_non_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can remove folders whatever their parent's mode"
)


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


def _tree(root: Path, *parts: str) -> Path:
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    (p / "file.bin").write_text("x")
    return p


@requires_non_root
def test_a_folder_that_cannot_be_removed_is_reported_and_keeps_the_profile(env):
    """The data folder is only deleted when all of it goes; a locked folder leaves it behind."""
    cfg, tmp = env
    data = paths.profiles_dir() / "gone"
    install = _tree(data, "install")  # an un-ticked tree, so it is passed as ``keep``
    locked_parent = data / "locked"
    stuck = _tree(locked_parent, "stuck")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])

    locked_parent.chmod(0o500)
    try:
        result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    finally:
        locked_parent.chmod(0o700)

    assert (install / "file.bin").exists()
    assert stuck.is_dir()
    warning = next((w for w in result.warnings if str(stuck) in w), None)
    assert warning is not None, result.warnings
    assert "could not be removed" in warning and "remove it by hand" in warning, warning
    assert "Permission denied" in warning, warning
    assert result.data_dir_removed is None, "a partly removed data folder was reported as removed"
    assert result.profile_kept and "gone" in cfg.profiles


@requires_non_root
def test_an_unwritable_parent_on_the_plain_path_still_warns_at_the_tree(env):
    """Without a keep list ``shutil.rmtree`` raises and the existing handling reports it."""
    cfg, tmp = env
    games = tmp / "Games"
    install = _tree(games, "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])

    games.chmod(0o500)
    try:
        result = removal.remove_game(
            cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
        )
    finally:
        games.chmod(0o700)

    assert install.is_dir()
    assert any("could not remove install_dir" in w for w in result.warnings), result.warnings
    assert result.trees_removed == []
    assert result.profile_kept and "gone" in cfg.profiles


def test_a_mounted_drive_is_still_reported_as_before(env, fake_mount):
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

    assert (mount / "sentinel.dat").exists()
    assert any("separate mounted drive" in w and str(mount) in w for w in result.warnings), (
        result.warnings
    )
    assert result.trees_removed == []
    assert result.profile_kept


@requires_non_root
def test_only_the_deepest_stuck_folder_is_reported(env):
    """Every ancestor of a stuck folder also fails to rmdir; one warning, not a chain of them."""
    cfg, tmp = env
    data = paths.profiles_dir() / "gone"
    install = _tree(data, "install")
    locked_parent = data / "locked"
    _tree(locked_parent, "stuck")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])

    locked_parent.chmod(0o500)
    try:
        result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    finally:
        locked_parent.chmod(0o700)

    stuck_warnings = [w for w in result.warnings if "could not be removed" in w]
    assert len(stuck_warnings) == 1, stuck_warnings
    assert str(locked_parent / "stuck") in stuck_warnings[0]
