# SPDX-License-Identifier: GPL-3.0-or-later
"""C4 (conflict review 11-21): a folder set only under ``extra`` is the game's folder.

The backends read ``prefix``/``install_dir`` from the top level, else from ``extra``
(``location._setting``, #1385). These readers decided something from the top level only, so a
game whose folder sits only in ``extra`` (an old or hand-edited config) got the wrong answer.
"""

from __future__ import annotations

from nadare.core import gamefinder, process, removal, safetycheck, sessionfiles

STAGE = ("drive_c", "Games", ".nadare", "nikke")


def test_sessionfiles_sweep_finds_a_session_in_an_extra_only_prefix(tmp_path):
    prefix = tmp_path / "games" / "NIKKE" / "prefix"
    session = prefix.joinpath(*STAGE, "session-1", "session.json")
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    profiles = {"nikke": {"backend": "launcher-nikke", "extra": {"prefix": str(prefix)}}}
    assert sessionfiles.find_leftovers(profiles, tmp_path / "profiles") == [session]


def test_run_env_sets_wineprefix_from_an_extra_only_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    env = process.build_env({"backend": "wine", "extra": {"prefix": str(prefix)}}, base={})
    assert env["WINEPREFIX"] == str(prefix)


def test_run_env_top_level_prefix_still_wins(tmp_path):
    top, other = tmp_path / "top", tmp_path / "other"
    profile = {"prefix": str(top), "extra": {"prefix": str(other)}}
    assert process.build_env(profile, base={})["WINEPREFIX"] == str(top)


def test_safetycheck_roots_include_an_extra_only_install_folder(tmp_path):
    install = tmp_path / "Game"
    roots = safetycheck._roots({"backend": "wine", "extra": {"install_dir": str(install)}}, "g")
    assert install in roots


def test_gamefinder_an_extra_only_install_folder_holds_the_game(tmp_path):
    install = tmp_path / "BD2"
    install.mkdir()
    profile = {
        "backend": "direct-bd2",
        "prefix": str(tmp_path / "gone-prefix"),
        "extra": {"install_dir": str(install)},
    }
    # The prefix is gone but the game files are there: not a broken install.
    assert gamefinder._game_files_missing(profile) is False


def test_removal_extra_install_elsewhere_is_not_the_add_game_shape(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Game"
    profile = {
        "backend": "wine",
        "prefix": str(home / "prefix"),
        "owned_trees": ["prefix"],
        "extra": {"install_dir": str(tmp_path / "elsewhere")},
    }
    # The game folder is somewhere else, so Nadare's wrapper is not the game's own folder.
    assert removal._wrapper_folders(profile, "Game") == []


def test_removal_extra_prefix_is_read_for_the_add_game_shape(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Game"
    profile = {
        "backend": "wine",
        "owned_trees": ["prefix", "install_dir"],
        "extra": {"prefix": str(home / "prefix"), "install_dir": str(home / "install")},
    }
    assert removal._wrapper_folders(profile, "Game") == [str(home)]
