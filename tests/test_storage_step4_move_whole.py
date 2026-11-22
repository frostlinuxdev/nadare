# SPDX-License-Identifier: GPL-3.0-or-later
"""#1372 phase 1 step 4: a whole-folder Move only takes the folder ``location`` calls the game's.

``relink.managed_home`` may answer only with :func:`location.game_folder`'s folder for a catalog
game; custom Wine games keep the ownership rule alone (location does not model them).
"""

from __future__ import annotations

from pathlib import Path

from nadare.core import location, relink


def _home(root: Path) -> Path:
    (root / "prefix" / "drive_c").mkdir(parents=True)
    (root / "prefix" / "system.reg").write_text("reg")
    (root / "install").mkdir()
    return root


def _profile(kind: str, home: Path, dirs: list[str]) -> dict:
    return {
        "backend": kind,
        "title": "Game",
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
        "owned_dirs": dirs,
    }


def test_a_catalog_game_folder_moves_whole(tmp_path):
    home = _home(tmp_path / "lib" / "Nadare" / "Aether Gazer")
    profile = _profile("direct-aethergazer", home, [str(home)])
    assert location.game_folder(profile, "aether") == home
    assert relink.managed_home(profile) == home


def test_a_catalog_game_with_two_recorded_folders_moves_only_its_prefix(tmp_path):
    """location never guesses between several folders, so neither does a whole-folder Move."""
    home = _home(tmp_path / "lib" / "Nadare" / "Aether Gazer")
    other = tmp_path / "lib2" / "Nadare" / "Aether Gazer"
    other.mkdir(parents=True)
    profile = _profile("direct-aethergazer", home, [str(home), str(other)])
    assert location.game_folder(profile, "aether") is None
    assert relink.managed_home(profile) is None


def test_a_custom_wine_game_folder_still_moves_whole(tmp_path):
    """Add Game makes Nadare/<Game> for a Wine game too; location does not model it."""
    home = _home(tmp_path / "lib" / "Nadare" / "My Game")
    profile = _profile("wine", home, [str(home)])
    assert relink.managed_home(profile) == home
