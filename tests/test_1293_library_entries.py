# SPDX-License-Identifier: GPL-3.0-or-later
"""#1293 follow-up: a library entry named ``Nadare`` has ONE meaning everywhere.

b21 stored the folder the player picked (``X/Nadare``) as the library and put games in
``X/Nadare/Nadare/<Game>``. Since b22 a picked ``Nadare`` folder means its parent library ``X``
(games in ``X/Nadare/<Game>``), but only the install dialog and Move applied that: the stored
entry still meant itself to Find games, Settings > Add library still stored ``X/Nadare``, and
``sync_game`` re-added ``X/Nadare`` for every b21 game. Now:

- on start, a stored ``.../Nadare`` entry becomes its parent (the config only, nothing on disk);
- Add library maps a picked ``Nadare`` folder the same way;
- Find games and every "which library is this in" question also understand the b21 layout
  ``X/Nadare/Nadare/<Game>``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core import gamefolders, libraries
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    drive = tmp_path / "drive" / "Gacha games"
    drive.mkdir(parents=True)
    return Config.load(tmp_path / "config.json"), drive.resolve(), home


def _game(game_dir: Path, kind="direct-bd2", title="Brown Dust 2") -> Path:
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "game.exe").write_text("x")
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": kind,
                "title": title,
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    return game_dir


def _profile(cfg, name, game_dir, kind="direct-bd2", title="Brown Dust 2"):
    cfg.add_profile(
        name,
        backend=kind,
        title=title,
        install_dir=str(game_dir / "install"),
        prefix=str(game_dir / "prefix"),
        owned_dirs=[str(game_dir)],
        owned_trees=["install_dir", "prefix"],
    )


def _stored(cfg) -> list[str]:
    return [str(e["path"]) for e in cfg.data.get(libraries.CONFIG_KEY, [])]


def _snapshot(root: Path) -> dict[str, tuple]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for item in dirnames + filenames:
            p = Path(dirpath) / item
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


# ---- the stored entry -------------------------------------------------------------------------


def test_a_stored_nadare_entry_becomes_its_parent_on_start(env):
    cfg, drive, _home = env
    (drive / "Nadare").mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive / "Nadare")}]
    before = _snapshot(drive.parent)
    assert libraries.backfill(cfg) is True  # the caller saves
    assert _stored(cfg) == [str(drive)]
    assert _snapshot(drive.parent) == before  # nothing on disk changed


def test_both_entries_become_one(env):
    cfg, drive, _home = env
    (drive / "Nadare").mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}, {"path": str(drive / "Nadare")}]
    libraries.backfill(cfg)
    assert _stored(cfg) == [str(drive)]


def test_the_rewrite_keeps_the_order_and_other_entries(env, tmp_path):
    cfg, drive, _home = env
    other = (tmp_path / "other").resolve()
    other.mkdir()
    (drive / "Nadare").mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(other)}, {"path": str(drive / "Nadare")}]
    libraries.backfill(cfg)
    assert _stored(cfg) == [str(other), str(drive)]


def test_a_home_nadare_library_stays_as_it_is(env):
    """``~/Nadare``: home cannot be a library, so that folder stays a library of its own."""
    cfg, _drive, home = env
    (home / "Nadare").mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(home / "Nadare")}]
    libraries.backfill(cfg)
    assert _stored(cfg) == [str(home / "Nadare")]


def test_an_unplugged_nadare_entry_is_rewritten_too(env, tmp_path):
    """The meaning is about the name, not the disk: an unplugged drive keeps its library."""
    cfg, _drive, _home = env
    gone = tmp_path / "unplugged" / "Games" / "Nadare"
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(gone)}]
    libraries.backfill(cfg)
    assert _stored(cfg) == [str(gone.parent)]


def test_a_second_start_changes_nothing(env):
    cfg, drive, _home = env
    (drive / "Nadare").mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive / "Nadare")}]
    libraries.backfill(cfg)
    assert libraries.backfill(cfg) is False


def test_add_library_maps_a_picked_nadare_folder(env):
    cfg, drive, _home = env
    (drive / "Nadare").mkdir()
    assert libraries.add_library(cfg, drive / "Nadare") == drive
    assert _stored(cfg) == [str(drive)]


# ---- b21 games after the rewrite ---------------------------------------------------------------


def _b21_case(cfg, drive):
    """A b21 game in ``X/Nadare/Nadare`` and a b22 game in ``X/Nadare``, entry stored by b21."""
    b21 = _game(drive / "Nadare" / "Nadare" / "Brown Dust 2")
    b22 = _game(drive / "Nadare" / "Honkai Star Rail", kind="direct-hsr", title="Honkai: Star Rail")
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive / "Nadare")}]
    return b21, b22


def test_both_layouts_are_in_the_library_after_the_rewrite(env):
    cfg, drive, _home = env
    b21, b22 = _b21_case(cfg, drive)
    _profile(cfg, "bd2", b21)
    _profile(cfg, "hsr", b22, kind="direct-hsr", title="Honkai: Star Rail")
    libraries.backfill(cfg)
    for name in ("bd2", "hsr"):
        place = gamefolders.placement(cfg, name)
        assert place is not None and place.library == drive, name


def test_backfill_never_brings_the_nadare_entry_back(env):
    """``sync_game`` used to re-add ``game_dir.parent.parent`` (= ``X/Nadare`` for a b21 game)."""
    cfg, drive, _home = env
    b21, _b22 = _b21_case(cfg, drive)
    _profile(cfg, "bd2", b21)
    cfg.data.pop(libraries.CONFIG_KEY)  # a config from before libraries: backfill remembers them
    libraries.backfill(cfg)
    assert _stored(cfg) == [str(drive)]


def test_find_games_sees_both_layouts_in_one_library(env):
    cfg, drive, _home = env
    _b21_case(cfg, drive)
    libraries.backfill(cfg)
    found, _notes = libraries.scan(cfg, drive)
    assert sorted(c.title for c in found) == ["Brown Dust 2", "Honkai: Star Rail"]
    # The b21 games folder itself is never offered as a game.
    assert all(c.game_dir.name != libraries.NADARE_DIR for c in found)


def test_find_unmarked_sees_the_b21_layout_too(env):
    cfg, drive, _home = env
    game = drive / "Nadare" / "Nadare" / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / "install" / "BrownDust II.exe").write_text("x")
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    spec = libraries.GameSpec(
        kind="direct-bd2", title="Brown Dust 2", exes=("BrownDust II.exe",),
        install_root="drive_c/Games", install_subdir="BrownDust2",
    )
    found = libraries.find_unmarked(cfg, drive, [spec])
    assert [c.game_dir for c in found] == [game.resolve()]


def test_the_library_of_a_b21_game_folder_is_the_parent(env):
    _cfg, drive, _home = env
    assert libraries.library_root_for(drive / "Nadare" / "Nadare" / "G") == drive
    assert libraries.library_root_for(drive / "Nadare" / "G") == drive
    assert libraries.library_root_for(drive / "G") is None
