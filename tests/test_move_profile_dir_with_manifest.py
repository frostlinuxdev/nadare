# SPDX-License-Identifier: GPL-3.0-or-later
"""A game in ``profiles/<id>/`` still moves whole after Nadare wrote its manifest there.

Since the b22 folder rule every game folder gets ``nadare-game.json``. The whole-folder check
only allowed ``prefix`` and ``install``, so on flxpc (2026-11-14) Arknights showed "Move prefix
only..." and a folder picker instead of "Move game and prefix...". Anything else in the folder
(a backend's own state) still keeps the game on the prefix-only move.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import libraries, paths, relink, relocate


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data" / "profiles"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    return root


def _old(data: Path, ident: str, *, title: str) -> dict:
    home = data / ident
    (home / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (home / "prefix" / "system.reg").write_text("reg")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ" * 50)
    return {
        "backend": "arknights-yostar",
        "title": title,
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }


def test_nadares_own_manifest_does_not_block_the_whole_move(data):
    profile = _old(data, "arknights", title="Arknights")
    assert libraries.write_manifest(profile, data / "arknights") is not None
    assert relink.managed_home(profile) == data / "arknights"


def test_other_files_in_the_folder_still_block_it(data):
    profile = _old(data, "arknights", title="Arknights")
    (data / "arknights" / "session.json").write_text("{}")
    assert relink.managed_home(profile) is None


def test_the_manifest_moves_with_the_folder_and_still_describes_it(data, tmp_path):
    profile = _old(data, "arknights", title="Arknights")
    libraries.write_manifest(profile, data / "arknights")
    home = relink.managed_home(profile)
    library = tmp_path / "izuna"
    library.mkdir()
    new_home = relink.free_home(library, relink.home_name(profile, home), {})
    relocate.move_tree(home, new_home)
    relink.apply(profile, relink.plan_home_move(profile, new_home))
    assert profile["prefix"] == str(new_home / "prefix")
    manifest = json.loads((new_home / libraries.MANIFEST_NAME).read_text())
    assert (new_home / manifest["prefix"]).resolve() == Path(profile["prefix"]).resolve()
    assert (new_home / manifest["install"]).resolve() == Path(profile["install_dir"]).resolve()
