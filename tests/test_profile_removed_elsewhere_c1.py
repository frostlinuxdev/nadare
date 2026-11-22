# SPDX-License-Identifier: GPL-3.0-or-later
"""Any save drops a game another process removed, even after a local write into it (C1).

#1717 made a Settings save refuse when its game was removed elsewhere. Every other write into a
profile (play time, favourite, art, a backend recording a folder) still made the three-way merge
treat the profile as "changed here", and the next save wrote the removed game back. A profile that
this process loaded from disk and that is gone from disk now was removed elsewhere, whatever
changed locally.
"""

from __future__ import annotations

import json

import pytest

from nadare.core.config import Config


def _removed_elsewhere(tmp_path, *names: str):
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("ag", backend="wine", title="Aether Gazer")
    first.add_profile("bd2", backend="wine", title="Brown Dust 2")
    first.save()
    gui = Config.load(path)  # the window, loaded before the removal
    cli = Config.load(path)  # `nadare remove` in a terminal
    for name in names or ("ag",):
        cli.remove_profile(name)
    cli.save()
    return path, gui


def _on_disk(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["profiles"]


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda cfg: cfg.mark_played("ag"), id="play-time"),
        pytest.param(lambda cfg: cfg.set_favourite("ag", True), id="favourite"),
        pytest.param(lambda cfg: cfg.profiles["ag"].update(cover="/tmp/c.png"), id="art"),
        pytest.param(
            lambda cfg: cfg.profiles["ag"].update(owned_trees=["install", "prefix"]),
            id="location-record",
        ),
    ],
)
def test_a_local_write_does_not_bring_back_a_removed_game(tmp_path, write):
    path, gui = _removed_elsewhere(tmp_path)
    write(gui)
    gui.save()
    assert set(_on_disk(path)) == {"bd2"}
    assert "ag" not in gui.profiles


def test_the_active_profile_moves_off_the_removed_game(tmp_path):
    path, gui = _removed_elsewhere(tmp_path)
    gui.set_active("ag")
    gui.mark_played("ag")
    gui.save()
    assert gui.active == "bd2"
    assert json.loads(path.read_text(encoding="utf-8"))["active_profile"] == "bd2"


def test_a_write_into_a_game_still_on_disk_is_kept(tmp_path):
    path, gui = _removed_elsewhere(tmp_path)  # only "ag" went away
    gui.set_favourite("bd2", True)
    gui.mark_played("ag")
    gui.save()
    disk = _on_disk(path)
    assert set(disk) == {"bd2"}
    assert disk["bd2"]["favourite"] is True


def test_a_game_added_here_and_not_saved_yet_is_kept(tmp_path):
    """Not in the snapshot: the disk never had it, so its absence there means nothing."""
    path, gui = _removed_elsewhere(tmp_path)
    gui.add_profile("nte", backend="wine", title="Neverness to Everness")
    gui.save()
    assert set(_on_disk(path)) == {"bd2", "nte"}


def test_a_game_removed_and_re_added_elsewhere_is_kept(tmp_path):
    """Gone and back on disk: present on disk, so it is merged, not dropped."""
    path, gui = _removed_elsewhere(tmp_path)
    again = Config.load(path)
    again.add_profile("ag", backend="wine", title="Aether Gazer again")
    again.save()
    gui.set_favourite("ag", True)
    gui.save()
    disk = _on_disk(path)
    assert disk["ag"]["title"] == "Aether Gazer again"
    assert disk["ag"]["favourite"] is True


def test_a_later_save_does_not_drop_it_twice_or_fail(tmp_path):
    path, gui = _removed_elsewhere(tmp_path)
    gui.mark_played("ag")
    gui.save()
    gui.data["check_updates"] = False
    gui.save()
    assert set(_on_disk(path)) == {"bd2"}
    assert Config.load(path).data["check_updates"] is False
