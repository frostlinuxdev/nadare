# SPDX-License-Identifier: GPL-3.0-or-later
"""Removal safety tests ported from the deleted ``backend.uninstall()`` paths.

Since #1372 step 3, Settings Remove (``removal.remove_game``) is the only Uninstall.
Each test here replays a scenario that a backend's own ``uninstall()``/``purge`` used to
guard, now through ``remove_game`` and the same ``removable_trees``/``_rmtree_safely``
rules. Only scenarios no existing removal test covered were ported (the rest were
deleted with a covering test named in the step-3 report).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import paths, removal
from nadare.core.backends import Status
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


def _not_running(monkeypatch) -> None:
    """No real backend probe: removal only needs "installed, not running"."""

    class Fake:
        def status(self) -> Status:
            return Status("installed")

    monkeypatch.setattr(removal.backends, "create", lambda *a, **k: Fake())


def _fake_mount(monkeypatch, mount: Path) -> None:
    """Make ``os.path.ismount`` report exactly ``mount`` (tests cannot mount)."""
    real_ismount = os.path.ismount

    def ismount(path):
        return Path(os.fspath(path)) == mount or real_ismount(path)

    monkeypatch.setattr(removal.os.path, "ismount", ismount)


# ---- ported from tests/test_backends_arknights.py::TestVerifyAndLogin -----------------------
def test_a_prefix_nadare_recorded_outside_the_profile_is_removed(env, monkeypatch):
    """A prefix outside the profile data dir that this profile recorded when it made it
    (an imported/moved install) is removable."""
    cfg, tmp = env
    _not_running(monkeypatch)
    outside = tmp / "elsewhere"
    (outside / "drive_c").mkdir(parents=True)
    (outside / "drive_c" / "game.exe").write_text("MZ")
    cfg.profiles["gone"].update(
        prefix=str(outside),
        owned_trees=["prefix"],
        owned_paths={"prefix": str(outside.resolve())},
    )

    result = removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )

    assert not outside.exists()
    assert result.trees_removed == [outside.resolve()]


# ---- ported from tests/test_backends_arknights.py::TestUninstallSafety ----------------------
def test_a_mounted_folder_inside_the_data_dir_is_left_alone(env, monkeypatch):
    """Purging the profile data must not descend into a mounted drive that sits inside it."""
    cfg, tmp = env
    _not_running(monkeypatch)
    data = paths.profiles_dir() / "gone"
    docs = data / "elsewhere" / "Documents"
    docs.mkdir(parents=True)
    (docs / "thesis.txt").write_text("PERSONAL")
    prefix = tmp / "Prefixes" / "gone"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.profiles["gone"].update(prefix=str(prefix), owned_trees=["prefix"])
    _fake_mount(monkeypatch, docs.resolve())

    result = removal.remove_game(
        cfg,
        "gone",
        delete_data=True,
        delete_trees=["prefix"],
        applications_dir=tmp / "apps",
    )

    assert (docs / "thesis.txt").read_text() == "PERSONAL", "data on the mounted drive was deleted"
    assert docs.is_dir()
    assert any(
        "separate mounted drive" in w and str(docs) in w and "left alone" in w
        for w in result.warnings
    ), result.warnings
    assert result.data_dir_removed is None, "a partly removed data folder was reported as removed"


# ---- ported from tests/test_final_guards.py ------------------------------------------------
def test_the_saved_login_survives_a_plain_remove_and_goes_with_the_data(env, monkeypatch):
    """A plain Remove keeps the saved login; choosing the data (and the prefix) deletes it."""
    cfg, tmp = env
    _not_running(monkeypatch)
    data = paths.profiles_dir() / "gone"
    (data / "prefix" / "drive_c").mkdir(parents=True)
    session = data / "session.json"
    session.write_text('{"refresh_token": "live"}')

    first = removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")

    assert not first.profile_kept
    assert session.exists(), "a plain Remove deleted the saved login"

    cfg.add_profile("gone", backend="wine", title="Gone")
    second = removal.remove_game(
        cfg,
        "gone",
        delete_data=True,
        delete_trees=["prefix"],
        applications_dir=tmp / "apps",
    )

    assert not second.profile_kept
    assert not session.exists(), "deleting the data kept the saved login"
    assert not data.exists()
