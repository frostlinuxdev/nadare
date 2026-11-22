# SPDX-License-Identifier: GPL-3.0-or-later
"""#1417 phase 2e: folders under ``profiles/`` that no game uses any more.

They are offered in Settings, never deleted silently: they may hold saves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import paths, profilefolders
from nadare.core.location import NADARE_DIR


@pytest.fixture
def root() -> Path:
    folder = paths.profiles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _folder(root: Path, name: str, payload: int = 10) -> Path:
    d = root / name
    (d / "prefix" / "drive_c").mkdir(parents=True)
    (d / "prefix" / "drive_c" / "save.dat").write_bytes(b"s" * payload)
    return d


def _names(found) -> list[str]:
    return [item.path.name for item in found]


def test_a_folder_no_profile_uses_is_found_with_its_size(root):
    _folder(root, "gone", payload=1234)
    found = profilefolders.find({}, [])
    assert _names(found) == ["gone"]
    assert found[0].size == 1234


def test_a_profiles_own_folder_is_never_offered(root):
    _folder(root, "czn")
    _folder(root, "gone")
    found = profilefolders.find({"czn": {"backend": "stove-czn"}}, [])
    assert _names(found) == ["gone"]


def test_the_default_library_folder_is_never_offered(root):
    # profiles/Nadare holds the games installed into the default library.
    (root / NADARE_DIR / "Some Game").mkdir(parents=True)
    assert profilefolders.find({}, []) == []


def test_a_folder_another_profile_records_is_never_offered(root):
    # An imported or moved game can keep its prefix in another profile's old folder.
    old = _folder(root, "old-name")
    profiles = {"renamed": {"backend": "wine", "prefix": str(old / "prefix")}}
    assert profilefolders.find(profiles, []) == []


def test_a_folder_holding_another_games_install_dir_is_never_offered(root):
    old = _folder(root, "old")
    (old / "game").mkdir()
    profiles = {"other": {"backend": "wine", "install_dir": str(old / "game")}}
    assert profilefolders.find(profiles, []) == []


def test_a_user_library_inside_the_profiles_folder_is_never_offered(root):
    library = root / "mylib"
    (library / NADARE_DIR).mkdir(parents=True)
    assert profilefolders.find({}, [paths.profiles_dir(), library]) == []


def test_a_folder_holding_a_user_library_is_never_offered(root):
    holder = root / "holder"
    (holder / "lib" / NADARE_DIR).mkdir(parents=True)
    assert profilefolders.find({}, [holder / "lib"]) == []


def test_links_hidden_entries_and_files_are_never_offered(root, tmp_path):
    outside = tmp_path / "documents"
    outside.mkdir()
    (outside / "thesis.odt").write_text("keep")
    (root / "linked").symlink_to(outside)
    (root / ".hidden").mkdir()
    (root / "stray.txt").write_text("x")
    assert profilefolders.find({}, []) == []


def test_no_profiles_folder_means_nothing_found():
    assert not paths.profiles_dir().exists()
    assert profilefolders.find({}, []) == []


# ---- remove -----------------------------------------------------------------------------------
def test_remove_deletes_an_orphan(root):
    gone = _folder(root, "gone")
    assert profilefolders.remove(gone, {}, []) is True
    assert not gone.exists()


def test_remove_refuses_a_folder_a_profile_now_uses(root):
    # The list was shown, then the game was added again before Delete was pressed.
    gone = _folder(root, "gone")
    assert profilefolders.remove(gone, {"gone": {"backend": "wine"}}, []) is False
    assert gone.exists()


def test_remove_refuses_a_folder_another_profile_records(root):
    gone = _folder(root, "gone")
    profiles = {"x": {"backend": "wine", "prefix": str(gone / "prefix")}}
    assert profilefolders.remove(gone, profiles, []) is False
    assert gone.exists()


def test_remove_refuses_the_default_library_folder(root):
    library = root / NADARE_DIR
    (library / "Game").mkdir(parents=True)
    assert profilefolders.remove(library, {}, []) is False
    assert (library / "Game").exists()


def test_remove_refuses_a_link_and_never_touches_its_target(root, tmp_path):
    outside = tmp_path / "documents"
    outside.mkdir()
    (outside / "thesis.odt").write_text("keep")
    link = root / "linked"
    link.symlink_to(outside)
    assert profilefolders.remove(link, {}, []) is False
    assert (outside / "thesis.odt").read_text() == "keep"
    assert link.is_symlink()


def test_remove_refuses_anything_outside_the_profiles_folder(root, tmp_path):
    orphan = _folder(root, "gone")
    (orphan / "prefix" / "gone").mkdir()
    elsewhere = tmp_path / "elsewhere" / "gone"
    elsewhere.mkdir(parents=True)
    # Same name as the orphan: only the path given may count, never profiles/<its name>.
    assert profilefolders.remove(elsewhere, {}, []) is False
    assert elsewhere.exists() and orphan.exists()
    nested = orphan / "prefix" / "gone"
    assert profilefolders.remove(nested, {}, []) is False  # only direct children
    assert nested.exists() and orphan.exists()


def test_remove_refuses_a_traversal_name(root, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    sneaky = root.joinpath(*[".."] * len(root.relative_to(tmp_path).parts), "victim")
    assert sneaky.resolve() == victim.resolve()
    assert profilefolders.remove(sneaky, {}, []) is False
    assert victim.exists()


def test_the_default_library_does_not_hide_every_folder(root):
    # library_roots() lists profiles/ itself first (the default library): it holds every
    # folder there, so counting it would hide them all.
    _folder(root, "gone")
    assert _names(profilefolders.find({}, [paths.profiles_dir()])) == ["gone"]


def test_a_user_library_holding_the_profiles_folder_hides_everything(root):
    # A library added at Nadare's data folder: refuse rather than guess what is a game.
    _folder(root, "gone")
    assert profilefolders.find({}, [paths.profiles_dir().parent]) == []
