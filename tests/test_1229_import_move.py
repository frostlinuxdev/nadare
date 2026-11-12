# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: "Move into a library" for a game the user already had (an import)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import gamefolders, importmove, libraries, relink, relocate
from nadare.core.config import Config
from nadare.core.events import Cancel, Cancelled


@pytest.fixture()
def setup(tmp_path):
    config = Config.load(tmp_path / "config.json")
    old = tmp_path / "old-games"
    install = old / "Wuthering Waves"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "game.pak").write_bytes(b"pak" * 1000)
    prefix = old / "wuwa-prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves: Test",
        install_dir=str(install), prefix=str(prefix),
    )
    library = tmp_path / "Games"
    library.mkdir()
    return config, install, prefix, library


def test_the_plan_puts_both_folders_into_one_game_folder(setup):
    config, install, prefix, library = setup
    move = importmove.plan(config, "wuwa", library)
    home = library / "Nadare" / libraries.game_folder_name("Wuthering Waves: Test")
    assert move.home == home
    assert [(k, s, d) for k, s, d in move.trees] == [
        ("install_dir", install, home / "install"),
        ("prefix", prefix, home / "prefix"),
    ]


def test_run_and_commit_make_it_a_library_game(setup):
    config, install, prefix, library = setup
    move = importmove.plan(config, "wuwa", library)
    importmove.run(move)
    importmove.commit(config, "wuwa", move)
    profile = config.profiles["wuwa"]
    assert profile["install_dir"] == str(move.home / "install")
    assert profile["prefix"] == str(move.home / "prefix")
    assert (move.home / "install" / "Client" / "game.pak").read_bytes() == b"pak" * 1000
    assert (move.home / "prefix" / "drive_c").is_dir()
    assert (move.home / libraries.MANIFEST_NAME).is_file()
    assert library.resolve() in libraries.library_roots(config)
    place = gamefolders.placement(config, "wuwa")
    assert place.made_by_nadare and place.library == library.resolve()


def test_a_game_inside_its_prefix_moves_as_the_prefix_only(tmp_path):
    config = Config.load(tmp_path / "config.json")
    prefix = tmp_path / "nikke-prefix"
    (prefix / "drive_c" / "NIKKE").mkdir(parents=True)
    config.add_profile("nikke", backend="launcher-nikke", title="NIKKE", prefix=str(prefix))
    library = tmp_path / "Games"
    library.mkdir()
    move = importmove.plan(config, "nikke", library)
    assert [k for k, _, _ in move.trees] == ["prefix"]
    importmove.run(move)
    importmove.commit(config, "nikke", move)
    assert config.profiles["nikke"]["prefix"] == str(move.home / "prefix")
    assert (move.home / "prefix" / "drive_c" / "NIKKE").is_dir()


def test_a_library_game_is_not_an_import(setup, tmp_path):
    config, install, prefix, library = setup
    config.profiles["wuwa"]["owned_trees"] = ["install_dir", "prefix"]
    config.profiles["wuwa"]["owned_dirs"] = [str(install.parent)]
    with pytest.raises(relink.RelinkError, match="not an existing install"):
        importmove.plan(config, "wuwa", library)


def test_a_refused_library_is_refused(setup):
    config, *_ = setup
    with pytest.raises(relink.RelinkError, match="home folder"):
        importmove.plan(config, "wuwa", Path.home())


def test_a_library_inside_the_games_own_folder_is_refused(setup):
    config, install, prefix, library = setup
    with pytest.raises(relink.RelinkError, match="inside"):
        importmove.plan(config, "wuwa", install / "Client")


def test_a_missing_prefix_is_refused(setup):
    config, install, prefix, library = setup
    config.profiles["wuwa"]["prefix"] = str(prefix.parent / "gone")
    with pytest.raises(relink.RelinkError, match="prefix"):
        importmove.plan(config, "wuwa", library)


def test_another_games_files_inside_are_refused(setup):
    config, install, prefix, library = setup
    config.add_profile("other", backend="wine", title="Other", install_dir=str(install / "Client"))
    with pytest.raises(relink.RelinkError, match="Other"):
        importmove.plan(config, "wuwa", library)


def test_a_failed_second_move_puts_the_first_back(setup, monkeypatch):
    config, install, prefix, library = setup
    move = importmove.plan(config, "wuwa", library)
    real = relocate.move_tree

    def flaky(src, dst, **kwargs):
        if Path(src) == prefix:
            raise relocate.RelocateError("disk full")
        return real(src, dst, **kwargs)

    monkeypatch.setattr(relocate, "move_tree", flaky)
    with pytest.raises(relocate.RelocateError):
        importmove.run(move)
    assert (install / "Client" / "game.pak").is_file()  # back where it was
    assert not (move.home / "install").exists()
    assert config.profiles["wuwa"]["install_dir"] == str(install)


def test_a_cancel_after_a_copy_removes_only_the_copy(setup, monkeypatch):
    config, install, prefix, library = setup
    move = importmove.plan(config, "wuwa", library)
    import shutil

    def copied(src, dst, **kwargs):
        if Path(src) == prefix:
            raise Cancelled()
        shutil.copytree(src, dst)
        return relocate.Moved(renamed=False, kept_old=Path(src))

    monkeypatch.setattr(relocate, "move_tree", copied)
    with pytest.raises(Cancelled):
        importmove.run(move, cancel=Cancel())
    assert (install / "Client" / "game.pak").is_file()  # the original was never touched
    assert not (move.home / "install").exists()  # our copy is gone


def test_a_game_folder_inside_the_prefix_moves_with_it(tmp_path):
    config = Config.load(tmp_path / "config.json")
    prefix = tmp_path / "czn-prefix"
    game = prefix / "drive_c" / "Games" / "CZN"
    game.mkdir(parents=True)
    config.add_profile(
        "czn", backend="direct-bd2", title="Inside", prefix=str(prefix), install_dir=str(game)
    )
    library = tmp_path / "Games"
    library.mkdir()
    move = importmove.plan(config, "czn", library)
    assert [k for k, _, _ in move.trees] == ["prefix"]
    importmove.run(move)
    importmove.commit(config, "czn", move)
    assert config.profiles["czn"]["install_dir"] == str(
        move.home / "prefix" / "drive_c" / "Games" / "CZN"
    )
    assert config.profiles["czn"]["owned_trees"] == ["prefix"]


def test_after_the_move_nadare_owns_both_folders(setup):
    config, install, prefix, library = setup
    move = importmove.plan(config, "wuwa", library)
    importmove.run(move)
    importmove.commit(config, "wuwa", move)
    profile = config.profiles["wuwa"]
    assert profile["owned_trees"] == ["install_dir", "prefix"]
    assert profile["owned_dirs"] == [str(move.home)]
    assert profile["owned_paths"]["prefix"] == str((move.home / "prefix").resolve())
