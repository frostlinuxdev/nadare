# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage review round 1 (Flash, 2026-11-14): records and guards around game folders.

B3: startup wrote one game's manifest into another game's folder.
B4: after a crash while deleting the default prefix, Uninstall could never finish.
B5: a NUL in a stored path stopped Nadare at startup.
C3: another game's own ``profiles/<name>`` was not "already used" when it names no folder.
C4: in the Flatpak, a link inside a granted folder was accepted wherever it led.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nadare.core import flatpakfs, libraries, paths, prefixguard, removal
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home


def _default_game(cfg: Config, name: str, kind: str, title: str) -> Path:
    home = paths.profiles_dir() / name
    (home / "prefix" / "drive_c").mkdir(parents=True)
    (home / "install").mkdir()
    cfg.add_profile(
        name,
        backend=kind,
        title=title,
        prefix=str(home / "prefix"),
        install_dir=str(home / "install"),
        owned_trees=["install_dir", "prefix"],
    )
    return home


def test_startup_never_overwrites_another_games_manifest(env):
    cfg, _home = env
    beta = _default_game(cfg, "beta", "direct-bd2", "Beta Game")
    libraries.backfill(cfg)
    assert json.loads((beta / libraries.MANIFEST_NAME).read_text())["title"] == "Beta Game"
    cfg.add_profile(
        "alpha",
        backend="direct-aethergazer",
        title="Alpha Game",
        prefix=str(beta / "prefix"),
        owned_trees=["prefix"],
    )
    libraries.backfill(cfg)
    assert json.loads((beta / libraries.MANIFEST_NAME).read_text())["title"] == "Beta Game"


def test_a_crash_while_deleting_the_default_prefix_can_be_finished(env, tmp_path):
    cfg, _home = env
    prefix = paths.profiles_dir() / "gone" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.profiles["gone"]["removal_pending"] = {"prefix": str(prefix)}
    cfg.save()
    shutil.rmtree(prefix)  # the crash happened after the delete, before the profile went
    result = removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], applications_dir=tmp_path / "apps"
    )
    assert "gone" not in cfg.profiles
    assert not result.refused


def test_a_nul_in_a_stored_path_does_not_stop_startup(env):
    cfg, _home = env
    _default_game(cfg, "bd2", "direct-bd2", "Brown Dust 2")
    cfg.profiles["bd2"]["install_dir"] = str(paths.profiles_dir() / "bd2" / "x") + "\x00evil"
    libraries.backfill(cfg)  # must not raise


def test_another_games_own_folder_is_already_used(env):
    cfg, _home = env
    cfg.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")  # names no folder
    inside = paths.profiles_dir() / "czn" / "install"
    assert prefixguard.unsafe_reason(inside, profiles=cfg.profiles, own="newgame")
    assert prefixguard.unsafe_reason(inside, profiles=cfg.profiles, own="czn") == ""


def test_the_flatpak_refuses_a_link_that_leads_outside_the_granted_folders(env, monkeypatch):
    import tempfile

    # Tiny real paths outside /mnt, which the Flatpak deliberately grants.
    with tempfile.TemporaryDirectory(prefix="nf-", dir="/tmp") as folder:
        home = Path(folder)
        monkeypatch.setenv("HOME", str(home))
        games = home / "Games"
        games.mkdir()
        elsewhere = home / "Private"
        elsewhere.mkdir()
        (games / "host").symlink_to(elsewhere)
        monkeypatch.setattr(flatpakfs.sandbox, "in_flatpak", lambda: True)
        monkeypatch.setattr(flatpakfs, "_grants", lambda: ["~/Games", "/mnt"])
        assert flatpakfs.unreachable_reason(games / "real") is None
        assert flatpakfs.unreachable_reason(games / "host") is not None
