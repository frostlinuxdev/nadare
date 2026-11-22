# SPDX-License-Identifier: GPL-3.0-or-later
"""Guards from the storage and report work since b24 that no test pinned (b25 mutation sweep).

Each test failed against one mutant that survived the sweep (2026-11-16, `next` 0165c371):
L01/L02 the stored location record, L11 a game on its own drive, R05 the adopted folder in
Nadare's own folders, P03 report labels.
"""

from __future__ import annotations

import os

import pytest

from nadare.core import location, paths, removal, report

KIND = "direct-bd2"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    profiles = paths.profiles_dir()
    profiles.mkdir(parents=True)
    return profiles


def test_a_record_that_says_not_nadares_wins_over_what_the_disk_shows_now(tmp_path, root):
    # Recorded while profiles/bd2 was a link to the player's folder (never Nadare's, B1).
    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    (root / "bd2").symlink_to(elsewhere)
    profile = {"backend": KIND}
    location.refresh_record(profile, "bd2")
    assert profile[location.RECORD_KEY]["folder"] is None
    # The link is replaced by a real folder; the profile's keys did not change, so the
    # record is still current and its "not Nadare's" answer stands (the safe direction).
    (root / "bd2").unlink()
    (root / "bd2").mkdir()
    assert location.record_is_current(profile)
    assert location.game_folder(profile, "bd2") is None


def test_a_recorded_default_library_folder_is_checked_on_disk_again(tmp_path, root):
    # Recorded while profiles/bd2 was Nadare's own folder ...
    (root / "bd2").mkdir()
    profile = {"backend": KIND}
    location.refresh_record(profile, "bd2")
    assert profile[location.RECORD_KEY]["folder"] == str(root / "bd2")
    # ... then swapped for a link to the player's folder: the record must not vouch for it.
    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    (root / "bd2").rmdir()
    (root / "bd2").symlink_to(elsewhere)
    assert location.record_is_current(profile)
    assert location.game_folder(profile, "bd2") is None


def test_a_game_whose_folder_is_a_whole_drive_still_owns_what_is_inside(
    tmp_path, root, monkeypatch
):
    drive = tmp_path / "gamedrive"
    (drive / "Data").mkdir(parents=True)
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        location.os.path, "ismount", lambda p: os.fspath(p) == str(drive) or real_ismount(p)
    )
    profiles = {"other": {"backend": KIND, "owned_dirs": [str(drive)]}}
    claim = location.overlapping(profiles, drive / "Data", excluding="mine")
    assert claim is not None and claim.owner == "other"


def test_an_adopted_folder_opens_only_itself_inside_nadares_folders(root):
    game = root / location.NADARE_DIR / "BD2"
    (game / "install").mkdir(parents=True)
    neighbour = root / location.NADARE_DIR / "Other"
    neighbour.mkdir()
    profiles = {"bd2": {"backend": KIND, "owned_dirs": [str(game)]}}
    assert removal.tree_safety(profiles, "bd2", game / "install") == (True, "")
    safe, why = removal.tree_safety(profiles, "bd2", neighbour)
    assert not safe and "nadare's own directories" in why


def test_a_line_that_only_starts_with_a_label_word_is_still_redacted():
    text = "Nadare: 0.1.0b25\nNadare home is /home/nadare"
    out = report.redact(text, username="nadare", labels=report.REPORT_LABELS)
    first, second = out.split("\n")
    assert first == "Nadare: 0.1.0b25"
    assert "nadare" not in second.lower()
