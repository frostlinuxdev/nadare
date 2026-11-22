# SPDX-License-Identifier: GPL-3.0-or-later
"""``location.game_folder``: the one answer to "which folder is this game's own?" (#1372)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import location, paths


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    profiles = paths.profiles_dir()
    profiles.mkdir(parents=True)
    return profiles


KIND = "direct-bd2"


def test_one_owned_dirs_entry_in_a_library(tmp_path, root):
    game = tmp_path / "Lib" / "Nadare" / "BD2"
    assert location.game_folder({"backend": KIND, "owned_dirs": [str(game)]}, "bd2") == game


@pytest.mark.parametrize("dirs", [["/a/Nadare/X", "/b/Nadare/Y"], ["/a/NotNadare/X"]])
def test_several_or_odd_owned_dirs_are_never_guessed(root, dirs):
    assert location.game_folder({"backend": KIND, "owned_dirs": dirs}, "bd2") is None


def test_the_old_library_layout(tmp_path, root):
    home = tmp_path / "Lib" / "Nadare" / "BD2"
    profile = {
        "backend": KIND,
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }
    assert location.game_folder(profile, "bd2") == home


def test_a_default_library_folder_named_differently(root):
    home = root / "Brown Dust 2"
    profile = {
        "backend": KIND,
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }
    assert location.game_folder(profile, "my-bd2") == home


def test_the_games_own_profiles_folder(root):
    home = root / "bd2"
    profile = {
        "backend": KIND,
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
    }
    assert location.game_folder(profile, "bd2") == home
    assert location.game_folder({"backend": KIND}, "bd2") == home  # names no folder


def test_an_imported_game_has_no_folder_of_nadares(tmp_path, root):
    profile = {"backend": KIND, "install_dir": str(tmp_path / "Games" / "BD2")}
    assert location.game_folder(profile, "bd2") is None


def test_nadares_install_next_to_the_players_prefix_is_still_nadares(tmp_path, root):
    home = root / "bd2"
    profile = {
        "backend": KIND,
        "install_dir": str(home / "install"),
        "prefix": str(tmp_path / "my-prefixes" / "bd2"),
        "owned_trees": ["install_dir"],
    }
    assert location.game_folder(profile, "bd2") == home


def test_generic_kinds_have_no_game_folder(root):
    assert location.game_folder({"backend": "wine"}, "tool") is None


def test_a_nul_path_is_not_a_folder(root):
    profile = {"backend": KIND, "install_dir": "/x\x00y"}
    assert location.game_folder(profile, "bd2") == root / "bd2"  # the NUL path names nothing
    assert location.game_folder({"backend": KIND, "owned_dirs": ["/a/Nadare/\x00"]}, "b") is None


def test_default_root_override(tmp_path, root):
    other = tmp_path / "planned"
    assert location.game_folder({"backend": KIND}, "bd2", default_root=other) == Path(other) / "bd2"


def test_a_linked_profiles_folder_is_never_nadares(tmp_path, root):
    documents = tmp_path / "Documents"
    (documents / "prefix").mkdir(parents=True)
    (root / "linked").symlink_to(documents)
    profile = {"backend": KIND, "prefix": str(root / "linked" / "prefix")}
    assert location.game_folder(profile, "linked") is None


def test_uninstall_tidying_agrees_with_the_answer_for_a_mixed_game(tmp_path, root):
    """Nadare's install, the player's prefix, no records: not Nadare's folder (review of #1374).

    Uninstall still tidies the game's own profiles/<name> once it is empty (#1366 s21): that
    only ever rmdirs an empty folder, it does not make the folder the game's own.
    """
    from nadare.core import removal

    profile = {
        "backend": KIND,
        "install_dir": str(root / "mixed" / "install"),
        "prefix": str(tmp_path / "pfx-mixed"),
    }
    assert location.game_folder(profile, "mixed") is None
    assert removal._wrapper_folders(profile, "mixed", others={}) == [str(root / "mixed")]


def test_a_folder_another_profile_also_claims_is_never_tidied(root):
    from nadare.core import removal

    home = root / "shared"
    record = {
        "backend": KIND,
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }
    profiles = {"a": dict(record), "b": dict(record)}
    assert location.game_folder(profiles["a"], "a") == home
    assert removal._wrapper_folders(profiles["a"], "a", others=profiles) == []
    assert removal._wrapper_folders(profiles["a"], "a", others={"a": profiles["a"]}) == [str(home)]


# ---- step 2: "used by another game" ------------------------------------------------------
def test_claims_cover_every_record_of_another_game(tmp_path, root):
    lib = tmp_path / "Lib" / "Nadare" / "BD2"
    profiles = {
        "bd2": {"backend": KIND, "owned_dirs": [str(lib)], "install_dir": str(lib / "install")},
        "czn": {"backend": "stove-czn"},  # names no folder: its profiles/<name> still counts
        "tool": {"backend": "wine", "extra": {"prefix": str(tmp_path / "pfx")}},
    }
    sources = {(c.owner, c.source) for c in location.claims(profiles)}
    assert ("bd2", "owned_dir") in sources and ("bd2", "path") in sources
    assert ("czn", "profiles") in sources
    assert ("tool", "path") in sources  # a folder set only under extra


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("inside", "inside"), ("contains", "contains"), ("either", "both")],
)
def test_overlapping_directions(tmp_path, root, direction, expected):
    game = tmp_path / "Games" / "G"
    profiles = {"g": {"backend": "wine", "install_dir": str(game)}}
    inside = location.overlapping(profiles, game / "sub", excluding=None, direction=direction)
    contains = location.overlapping(profiles, game.parent, excluding=None, direction=direction)
    got = {"inside": inside is not None, "contains": contains is not None}
    want = {
        "inside": {"inside": True, "contains": False},
        "contains": {"inside": False, "contains": True},
        "both": {"inside": True, "contains": True},
    }[expected]
    assert got == want


def test_a_broad_recorded_folder_only_counts_for_deletion(tmp_path, root, monkeypatch):
    home = tmp_path / "home"
    (home / "Games").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    profiles = {"old": {"backend": "wine", "prefix": str(home)}}  # an ancient profile: ~ as prefix
    target = home / "Games" / "New"
    assert location.overlapping(profiles, target, excluding=None) is None
    assert location.overlapping(profiles, target, excluding=None, ignore_broad=False) is not None


# ---- review of #1376 ----------------------------------------------------------------------
def test_a_linked_profiles_folder_still_protects_where_it_leads(tmp_path, root):
    real = tmp_path / "Lib" / "Nadare" / "Moved"
    (real / "prefix" / "drive_c").mkdir(parents=True)
    (root / "moved").symlink_to(real)
    profiles = {"moved": {"backend": "wine", "prefix": str(root / "moved" / "prefix")}}
    for path in (real / "prefix", real / "NewGame", real):
        assert location.overlapping(profiles, path, excluding="new") is not None, path


def test_a_recorded_link_inside_a_folder_still_counts_as_inside_it(tmp_path, root):
    game = tmp_path / "Lib" / "Nadare" / "Game"
    game.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (game / "prefix").symlink_to(elsewhere)
    profiles = {"p": {"backend": "wine", "prefix": str(game / "prefix")}}
    claim = location.overlapping(profiles, game, excluding="q", direction="contains")
    assert claim is not None and claim.owner == "p"


@pytest.mark.parametrize("damaged", ["/", "~"])
def test_a_broad_owned_dirs_entry_is_ignored(tmp_path, root, monkeypatch, damaged):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    profiles = {"b": {"backend": KIND, "owned_dirs": [damaged]}}
    target = tmp_path / "Lib" / "Nadare" / "A" / "prefix"
    assert location.overlapping(profiles, target, excluding="a", ignore_broad=False) is None


# ---- re-check of #1376 --------------------------------------------------------------------
def test_a_link_inside_a_game_folder_under_a_linked_library_root(tmp_path, root):
    """Bazzite/Silverblue: /home is a link; a resolved target must still meet a written claim."""
    disk = tmp_path / "mnt" / "disk" / "lib"
    (disk / "Nadare" / "G").mkdir(parents=True)
    store = tmp_path / "store"
    store.mkdir()
    (disk / "Nadare" / "G" / "prefix").symlink_to(store)
    linked_lib = tmp_path / "data-lib"
    linked_lib.symlink_to(disk)
    profiles = {"p": {"backend": "wine", "prefix": str(linked_lib / "Nadare" / "G" / "prefix")}}
    resolved_target = (linked_lib / "Nadare" / "G").resolve()
    assert location.overlapping(profiles, resolved_target, excluding="q", direction="contains")


def test_a_game_folder_on_its_own_drive_still_counts(tmp_path, root, monkeypatch):
    game = tmp_path / "Lib" / "Nadare" / "G"
    (game / "install").mkdir(parents=True)
    monkeypatch.setattr(location.os.path, "ismount", lambda p: str(p) == str(game))
    profiles = {"b": {"backend": "wine", "owned_dirs": [str(game)]}}  # only owned_dirs covers it
    assert location.overlapping(profiles, game / "install", excluding="a", ignore_broad=False)


def test_a_profiles_folder_linked_to_home_does_not_claim_all_of_home(tmp_path, root, monkeypatch):
    home = tmp_path / "home"
    (home / "Games").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    (root / "h").symlink_to(home)
    profiles = {"h": {"backend": "wine"}}
    assert location.overlapping(profiles, home / "Games" / "New", excluding="x") is None
