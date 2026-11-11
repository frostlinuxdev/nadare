# SPDX-License-Identifier: GPL-3.0-or-later
"""Game libraries the Steam way: roots, a manifest per game, scan and adopt."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core import config as config_mod
from nadare.core import libraries, paths
from nadare.core.config import Config

KIND = "direct-bd2"  # a catalog game: one profile per game


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = Config.load(tmp_path / "config.json")
    return cfg, home


def _library_game(root: Path, folder: str = "Brown Dust 2", **manifest) -> Path:
    game_dir = root / libraries.NADARE_DIR / folder
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "game.exe").write_text("x")
    data = {
        "schema": libraries.MANIFEST_SCHEMA,
        "backend": KIND,
        "title": "Brown Dust 2",
        "install": "install",
        "made_by_nadare": ["install"],
    }
    data.update(manifest)
    (game_dir / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    return game_dir


# ---- roots ----------------------------------------------------------------------------------


def test_the_default_library_is_always_first(env):
    cfg, _home = env
    assert libraries.library_roots(cfg) == [paths.profiles_dir().resolve()]


def test_a_non_empty_folder_can_be_a_library(env):
    cfg, home = env
    lib = home / "Games"
    lib.mkdir()
    (lib / "someone else's file.txt").write_text("x")
    added = libraries.add_library(cfg, lib)
    assert added == lib.resolve()
    assert lib.resolve() in libraries.library_roots(cfg)


def test_the_same_library_twice_is_refused(env):
    cfg, home = env
    lib = home / "Games"
    lib.mkdir()
    libraries.add_library(cfg, lib)
    with pytest.raises(libraries.LibraryError, match="already a library"):
        libraries.add_library(cfg, str(lib) + "/")


def test_the_default_library_cannot_be_added_again(env):
    cfg, _home = env
    with pytest.raises(libraries.LibraryError, match="already a library"):
        libraries.add_library(cfg, paths.profiles_dir())


@pytest.mark.parametrize("where", ["/", "home", "Documents"])
def test_unsafe_places_are_refused(env, where):
    cfg, home = env
    target = Path("/") if where == "/" else home if where == "home" else home / where
    target.mkdir(exist_ok=True)
    with pytest.raises(libraries.LibraryError):
        libraries.add_library(cfg, target)


def test_a_drive_root_is_allowed_as_a_library(env, monkeypatch):
    """Games go in <root>/Nadare/<Game>, so the top of a drive is fine as a library."""
    cfg, home = env
    drive = home / "mnt" / "games-drive"
    drive.mkdir(parents=True)
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == drive or real(p))
    assert libraries.add_library(cfg, drive) == drive.resolve()


def test_removing_a_library_forgets_it_and_touches_no_files(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    libraries.add_library(cfg, lib)
    assert libraries.remove_library(cfg, lib) is True
    assert lib.resolve() not in libraries.library_roots(cfg)
    assert (game_dir / "install" / "game.exe").is_file()
    assert (game_dir / libraries.MANIFEST_NAME).is_file()


def test_a_removed_library_with_games_in_it_can_be_added_back(env):
    """The Drop OSS trap: forget a folder, then it is refused for not being empty."""
    cfg, home = env
    lib = home / "Games"
    _library_game(lib)
    libraries.add_library(cfg, lib)
    libraries.remove_library(cfg, lib)
    assert libraries.add_library(cfg, lib) == lib.resolve()


def test_the_default_library_cannot_be_removed(env):
    cfg, _home = env
    with pytest.raises(libraries.LibraryError, match="default"):
        libraries.remove_library(cfg, paths.profiles_dir())


def test_libraries_survive_a_save_and_load(env, tmp_path):
    cfg, home = env
    lib = home / "Games"
    lib.mkdir()
    libraries.add_library(cfg, lib)
    cfg.save()
    again = Config.load(tmp_path / "config.json")
    assert lib.resolve() in libraries.library_roots(again)
    assert "libraries" in config_mod.KNOWN_TOP_KEYS


def test_a_damaged_libraries_entry_is_ignored(env):
    cfg, home = env
    cfg.data["libraries"] = [{"path": "relative/path"}, "not a dict", {"path": 3}, {}]
    assert libraries.library_roots(cfg) == [paths.profiles_dir().resolve()]


# ---- manifest -------------------------------------------------------------------------------


def test_a_manifest_round_trips_with_relative_paths(env):
    cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    profile = {
        "backend": KIND,
        "title": "Brown Dust 2",
        "install_dir": str(game_dir / "install"),
        "owned_trees": ["install_dir"],
        "owned_dirs": [str(game_dir)],
    }
    written = libraries.write_manifest(profile, game_dir)
    assert written == game_dir / libraries.MANIFEST_NAME
    raw = json.loads(written.read_text())
    assert raw["install"] == "install"
    assert str(home) not in written.read_text()  # relative only
    manifest = libraries.read_manifest(game_dir)
    assert manifest.kind == KIND
    assert manifest.install_dir == game_dir / "install"
    assert manifest.prefix is None
    assert manifest.made_by_nadare == ("install",)


def test_a_manifest_is_not_written_for_a_folder_outside_the_game_folder(env):
    cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "BD2"
    game_dir.mkdir(parents=True)
    profile = {"backend": KIND, "title": "BD2", "install_dir": str(home / "elsewhere")}
    assert libraries.write_manifest(profile, game_dir) is None
    assert not (game_dir / libraries.MANIFEST_NAME).exists()


# ---- scan -----------------------------------------------------------------------------------


def test_scan_finds_a_game_that_is_not_in_the_library(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    found, notes = libraries.scan(cfg, lib)
    assert [c.game_dir for c in found] == [game_dir]
    assert found[0].kind == KIND
    assert notes == []


def test_scan_skips_a_game_a_profile_already_uses(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    cfg.add_profile("bd2", backend=KIND, install_dir=str(game_dir / "install"))
    found, _notes = libraries.scan(cfg, lib)
    assert found == []


def test_scan_skips_a_game_whose_kind_already_has_a_profile(env):
    cfg, home = env
    lib = home / "Games"
    _library_game(lib)
    cfg.add_profile("bd2", backend=KIND, install_dir=str(home / "other" / "install"))
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert any("already" in n for n in notes)


def test_scan_of_the_default_library_reads_profile_folders(env):
    cfg, _home = env
    game_dir = paths.profiles_dir() / "bd2"
    (game_dir / "install").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps({"schema": 1, "backend": KIND, "title": "BD2", "install": "install"})
    )
    found, _notes = libraries.scan(cfg, paths.profiles_dir())
    assert [c.game_dir for c in found] == [game_dir]


@pytest.mark.parametrize(
    "bad",
    [
        {"install": "/etc"},
        {"install": "../../outside"},
        {"backend": "script"},  # a generic kind runs commands: never from a file on disk
        {"backend": "wine"},
        {"backend": "no-such-kind"},
        {"schema": 99},
        {"title": 5},
        {"made_by_nadare": "install"},
    ],
)
def test_a_bad_manifest_is_skipped_with_a_note(env, bad):
    cfg, home = env
    lib = home / "Games"
    _library_game(lib, **bad)
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert len(notes) == 1


def test_a_manifest_that_is_not_json_or_too_big_is_skipped(env):
    cfg, home = env
    lib = home / "Games"
    one = _library_game(lib, folder="One")
    (one / libraries.MANIFEST_NAME).write_text("{not json")
    two = _library_game(lib, folder="Two")
    (two / libraries.MANIFEST_NAME).write_text(" " * (libraries.MANIFEST_MAX_BYTES + 1))
    three = _library_game(lib, folder="Three")
    (three / libraries.MANIFEST_NAME).write_text("[1, 2]")
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert len(notes) == 3


def test_a_symlink_escaping_the_game_folder_is_refused(env):
    cfg, home = env
    lib = home / "Games"
    outside = home / "secret"
    outside.mkdir()
    game_dir = _library_game(lib, install="link")
    (game_dir / "link").symlink_to(outside)
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert notes


def test_a_symlinked_manifest_is_refused(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    real = home / "planted.json"
    real.write_text((game_dir / libraries.MANIFEST_NAME).read_text())
    (game_dir / libraries.MANIFEST_NAME).unlink()
    (game_dir / libraries.MANIFEST_NAME).symlink_to(real)
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert notes


def test_scan_of_a_missing_library_finds_nothing(env):
    cfg, home = env
    assert libraries.scan(cfg, home / "gone") == ([], [])


# ---- adopt ----------------------------------------------------------------------------------


def test_adopt_makes_a_profile_with_the_game_folders(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    (found,), _ = libraries.scan(cfg, lib)
    name = libraries.adopt(cfg, found)
    assert name == "bd2"
    prof = cfg.profiles[name]
    assert prof["backend"] == KIND
    assert prof["title"] == "Brown Dust 2"
    assert prof["install_dir"] == str(game_dir / "install")
    assert prof["owned_dirs"] == [str(game_dir)]
    assert prof["owned_trees"] == ["install_dir"]
    assert prof["owned_paths"] == {"install_dir": str((game_dir / "install").resolve())}
    assert libraries.scan(cfg, lib) == ([], [])


def test_adopt_picks_a_free_name(env):
    cfg, home = env
    cfg.add_profile("bd2", backend="wine", title="Something else")
    lib = home / "Games"
    _library_game(lib)
    (found,), _ = libraries.scan(cfg, lib)
    assert libraries.adopt(cfg, found) == "bd2-2"


def test_adopt_without_a_nadare_made_record_owns_nothing(env):
    cfg, home = env
    lib = home / "Games"
    _library_game(lib, made_by_nadare=[])
    (found,), _ = libraries.scan(cfg, lib)
    prof = cfg.profiles[libraries.adopt(cfg, found)]
    assert "owned_trees" not in prof
    assert "owned_paths" not in prof


# ---- record_new_game ------------------------------------------------------------------------


def test_a_new_game_in_a_library_gets_a_manifest(env):
    cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    profile = {
        "backend": KIND,
        "title": "Brown Dust 2",
        "install_dir": str(game_dir / "install"),
        "owned_dirs": [str(game_dir)],
        "owned_trees": ["install_dir"],
    }
    assert libraries.record_new_game(profile, "bd2") == game_dir / libraries.MANIFEST_NAME
    assert libraries.read_manifest(game_dir).made_by_nadare == ("install",)


def test_a_new_game_in_the_default_library_gets_a_manifest(env):
    cfg, _home = env
    game_dir = paths.profiles_dir() / "bd2"
    profile = {"backend": KIND, "title": "BD2", "install_dir": str(game_dir / "install")}
    assert libraries.record_new_game(profile, "bd2") == game_dir / libraries.MANIFEST_NAME


def test_no_manifest_for_an_imported_game_or_a_generic_kind(env):
    cfg, home = env
    imported = {"backend": KIND, "title": "BD2", "install_dir": str(home / "Games" / "BD2")}
    assert libraries.record_new_game(imported, "bd2") is None
    game_dir = home / "Games" / libraries.NADARE_DIR / "Tool"
    generic = {
        "backend": "wine",
        "title": "Tool",
        "install_dir": str(game_dir / "install"),
        "owned_dirs": [str(game_dir)],
    }
    assert libraries.record_new_game(generic, "tool") is None
    assert not game_dir.exists()


def test_a_manifest_that_cannot_be_written_is_not_an_error(env, monkeypatch):
    cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "BD2"
    profile = {
        "backend": KIND,
        "title": "BD2",
        "install_dir": str(game_dir / "install"),
        "owned_dirs": [str(game_dir)],
    }

    def boom(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(libraries.os, "replace", boom)
    assert libraries.record_new_game(profile, "bd2") is None


# ---- removal --------------------------------------------------------------------------------


def _made_game(cfg, home):
    from nadare.core import removal  # noqa: F401 - imported for the tests below

    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    install = game_dir / "install"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    values = {
        "backend": KIND,
        "title": "Brown Dust 2",
        "install_dir": str(install),
        "owned_dirs": [str(game_dir)],
        "owned_trees": ["install_dir"],
        "owned_paths": {"install_dir": str(install.resolve())},
    }
    cfg.add_profile("bd2", **values)
    cfg.save()
    libraries.record_new_game(values, "bd2")
    return game_dir


def test_removing_a_game_with_its_files_takes_the_manifest_and_folder(env, tmp_path):
    from nadare.core import removal

    cfg, home = env
    game_dir = _made_game(cfg, home)
    removal.remove_game(cfg, "bd2", delete_trees=["install_dir"], applications_dir=tmp_path / "a")
    assert not game_dir.exists()
    assert not game_dir.parent.exists()  # the empty Nadare folder goes too


def test_removing_a_game_but_keeping_its_files_keeps_the_manifest(env, tmp_path):
    """Kept files stay findable: adding the library back offers the game again."""
    from nadare.core import removal

    cfg, home = env
    game_dir = _made_game(cfg, home)
    removal.remove_game(cfg, "bd2", applications_dir=tmp_path / "a")
    assert (game_dir / libraries.MANIFEST_NAME).is_file()
    found, _ = libraries.scan(cfg, home / "Games")
    assert [c.game_dir for c in found] == [game_dir.resolve()]


def test_a_user_file_next_to_the_manifest_keeps_the_folder(env, tmp_path):
    from nadare.core import removal

    cfg, home = env
    game_dir = _made_game(cfg, home)
    (game_dir / "my notes.txt").write_text("mine")
    removal.remove_game(cfg, "bd2", delete_trees=["install_dir"], applications_dir=tmp_path / "a")
    assert (game_dir / "my notes.txt").is_file()


# ---- sync and backfill (step 3) -------------------------------------------------------------


def _old_game(cfg, home, name="bd2", lib="Games"):
    """A game an older Nadare installed into a picked library: no manifest, no library entry."""
    game_dir = home / lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    cfg.add_profile(
        name,
        backend=KIND,
        title="Brown Dust 2",
        install_dir=str(game_dir / "install"),
        owned_dirs=[str(game_dir)],
        owned_trees=["install_dir"],
    )
    return game_dir


def test_backfill_writes_the_manifest_and_remembers_the_library(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    assert libraries.backfill(cfg) is True
    assert libraries.read_manifest(game_dir).kind == KIND
    assert (home / "Games").resolve() in libraries.library_roots(cfg)


def test_backfill_twice_changes_nothing_the_second_time(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    libraries.backfill(cfg)
    stamp = (game_dir / libraries.MANIFEST_NAME).stat().st_mtime_ns
    assert libraries.backfill(cfg) is False
    assert (game_dir / libraries.MANIFEST_NAME).stat().st_mtime_ns == stamp


def test_backfill_skips_a_game_whose_folder_is_gone(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    import shutil

    shutil.rmtree(game_dir)
    libraries.backfill(cfg)
    assert not game_dir.exists()  # never recreated for a missing game


def test_backfill_leaves_generic_and_imported_games_alone(env):
    cfg, home = env
    imported = home / "Old" / "BD2"
    imported.mkdir(parents=True)
    cfg.add_profile("bd2", backend=KIND, title="BD2", install_dir=str(imported))
    tool = home / "T" / libraries.NADARE_DIR / "Tool"
    (tool / "install").mkdir(parents=True)
    cfg.add_profile(
        "tool", backend="wine", install_dir=str(tool / "install"), owned_dirs=[str(tool)]
    )
    assert libraries.backfill(cfg) is False
    assert not list(home.rglob(libraries.MANIFEST_NAME))
    assert libraries.library_roots(cfg) == [paths.profiles_dir().resolve()]


def test_sync_rewrites_a_manifest_that_no_longer_matches(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    libraries.sync_game(cfg, "bd2")
    (game_dir / "Game").mkdir()
    cfg.profiles["bd2"]["install_dir"] = str(game_dir / "Game")
    libraries.sync_game(cfg, "bd2")
    assert libraries.read_manifest(game_dir).install_dir == (game_dir / "Game").resolve()


def test_sync_after_a_move_remembers_the_new_library(env):
    cfg, home = env
    _old_game(cfg, home)
    libraries.sync_game(cfg, "bd2")
    new_dir = home / "SSD" / libraries.NADARE_DIR / "Brown Dust 2"
    (new_dir / "install").mkdir(parents=True)
    cfg.profiles["bd2"].update(install_dir=str(new_dir / "install"), owned_dirs=[str(new_dir)])
    assert libraries.sync_game(cfg, "bd2") is True
    assert (home / "SSD").resolve() in libraries.library_roots(cfg)
    assert libraries.read_manifest(new_dir) is not None


def test_a_bad_manifest_on_disk_is_replaced_by_sync(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    (game_dir / libraries.MANIFEST_NAME).write_text("{broken")
    libraries.sync_game(cfg, "bd2")
    assert libraries.read_manifest(game_dir) is not None


# ---- step 4: games whose manifest is gone -----------------------------------------------------

SPEC = libraries.GameSpec(
    kind=KIND,
    title="Brown Dust 2",
    exes=("BrownDust II.exe",),
    install_root="drive_c/Games",
    install_subdir="BrownDust2",
)


def _bare_game(root: Path, folder: str = "Brown Dust 2", where: str = "install") -> Path:
    """A Nadare-shaped game folder with the game's program but no manifest."""
    game_dir = root / libraries.NADARE_DIR / folder
    if where == "install":
        (game_dir / "install" / "Bin").mkdir(parents=True)
        (game_dir / "install" / "Bin" / "BrownDust II.exe").write_text("x")
    else:
        inner = game_dir / "prefix" / "drive_c" / "Games" / "BrownDust2"
        inner.mkdir(parents=True)
        (inner / "BrownDust II.exe").write_text("x")
    return game_dir


def test_match_game_finds_the_program_in_the_install_folder(env):
    _cfg, home = env
    game_dir = _bare_game(home / "Games")
    found = libraries.match_game(game_dir, SPEC)
    assert found == (game_dir / "install", None)


def test_match_game_finds_the_program_inside_the_prefix(env):
    _cfg, home = env
    game_dir = _bare_game(home / "Games", where="prefix")
    install, prefix = libraries.match_game(game_dir, SPEC)
    assert prefix == game_dir / "prefix"


def test_match_game_says_no_for_another_game(env):
    _cfg, home = env
    game_dir = _bare_game(home / "Games")
    other = libraries.GameSpec(kind="direct-pgr", title="PGR", exes=("PGR.exe",))
    assert libraries.match_game(game_dir, other) is None


def test_find_unmarked_suggests_a_folder_that_lost_its_manifest(env):
    cfg, home = env
    game_dir = _bare_game(home / "Games")
    found = libraries.find_unmarked(cfg, home / "Games", [SPEC])
    assert [(s.game_dir, s.kind) for s in found] == [(game_dir.resolve(), KIND)]
    assert found[0].made_by_nadare == ()  # a guess never claims Nadare made the files


def test_find_unmarked_skips_marked_used_and_already_listed_games(env):
    cfg, home = env
    lib = home / "Games"
    _library_game(lib, folder="Marked")  # has a manifest: scan() handles it
    used = _bare_game(lib, folder="Used")
    cfg.add_profile("x", backend="wine", install_dir=str(used / "install"))
    assert libraries.find_unmarked(cfg, lib, [SPEC]) == []
    _bare_game(lib, folder="Free")
    cfg.add_profile("bd2", backend=KIND, install_dir=str(home / "elsewhere"))
    assert libraries.find_unmarked(cfg, lib, [SPEC]) == []  # one profile per game


def test_find_unmarked_never_suggests_a_generic_kind(env):
    cfg, home = env
    _bare_game(home / "Games")
    generic = libraries.GameSpec(kind="wine", title="Any", exes=("BrownDust II.exe",))
    assert libraries.find_unmarked(cfg, home / "Games", [generic]) == []


def test_find_unmarked_ignores_a_symlinked_game_folder(env):
    cfg, home = env
    real = _bare_game(home / "Elsewhere")
    lib = home / "Games" / libraries.NADARE_DIR
    lib.mkdir(parents=True)
    (lib / "Brown Dust 2").symlink_to(real)
    assert libraries.find_unmarked(cfg, home / "Games", [SPEC]) == []


def test_adopting_a_guess_owns_nothing(env):
    cfg, home = env
    _bare_game(home / "Games")
    (found,) = libraries.find_unmarked(cfg, home / "Games", [SPEC])
    prof = cfg.profiles[libraries.adopt(cfg, found)]
    assert "owned_trees" not in prof
    assert prof["owned_dirs"] == [str(found.game_dir)]


def test_reusable_folder_in_a_library(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _bare_game(lib)
    reuse = libraries.reusable_folder(cfg, game_dir, SPEC)
    assert reuse is not None and reuse.made_by_nadare == ()
    cfg.add_profile("x", backend="wine", install_dir=str(game_dir / "install"))
    assert libraries.reusable_folder(cfg, game_dir, SPEC) is None  # someone uses it


def test_reusable_folder_trusts_a_matching_manifest(env):
    cfg, home = env
    game_dir = _library_game(home / "Games")  # manifest, made_by_nadare install
    reuse = libraries.reusable_folder(cfg, game_dir, SPEC)
    assert reuse.made_by_nadare == ("install",)
    other = libraries.GameSpec(kind="direct-pgr", title="PGR", exes=("PGR.exe",))
    assert libraries.reusable_folder(cfg, game_dir, other) is None  # another game's folder


def test_an_empty_or_missing_folder_is_not_reused(env):
    cfg, home = env
    assert libraries.reusable_folder(cfg, home / "nope", SPEC) is None
    (home / "empty").mkdir()
    assert libraries.reusable_folder(cfg, home / "empty", SPEC) is None


# ---- round-1 review 2026-11-11 ------------------------------------------------------------


@pytest.mark.parametrize("made", [[{}], [["install"]], [None, "install"]])
def test_a_made_by_nadare_with_odd_items_is_skipped_with_a_note(env, made):
    cfg, home = env
    lib = home / "Games"
    _library_game(lib, made_by_nadare=made)
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert len(notes) == 1


def test_a_deeply_nested_manifest_is_skipped_with_a_note(env):
    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    depth = min(libraries.MANIFEST_MAX_BYTES // 2 - 1, 8000)
    (game_dir / libraries.MANIFEST_NAME).write_text("[" * depth + "]" * depth)
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert len(notes) == 1
    assert libraries.read_manifest(game_dir) is None


def test_write_manifest_does_not_follow_a_planted_temp_link(env):
    cfg, home = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    victim = home / "victim.txt"
    victim.write_text("precious")
    (game_dir / f".{libraries.MANIFEST_NAME}.tmp").symlink_to(victim)
    profile = {"backend": KIND, "title": "Brown Dust 2", "install_dir": str(game_dir / "install")}
    target = libraries.write_manifest(profile, game_dir)
    assert victim.read_text() == "precious"
    assert target is not None and not target.is_symlink()
    assert json.loads(target.read_text())["backend"] == KIND


def test_a_manifest_whose_folders_are_gone_is_not_reused(env):
    cfg, home = env
    game_dir = _library_game(home / "Games")
    import shutil

    shutil.rmtree(game_dir / "install")
    assert libraries.reusable_folder(cfg, game_dir, SPEC) is None


def test_scan_skips_a_manifest_whose_folders_are_gone(env):
    """Round 2: Find games offered (and adopted) a game whose files were deleted."""
    import shutil

    cfg, home = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    shutil.rmtree(game_dir / "install")
    found, notes = libraries.scan(cfg, lib)
    assert found == []
    assert len(notes) == 1


# ---- round 4 (b23): a removed library stays removed; an adopted default-library game ----------


def test_backfill_does_not_re_add_a_library_the_user_removed(env):
    cfg, home = env
    _old_game(cfg, home)
    libraries.backfill(cfg)  # the first start after the upgrade remembers ~/Games
    cfg.save()
    libraries.remove_library(cfg, home / "Games")  # Settings -> Libraries -> Remove
    cfg.save()
    again = Config.load(cfg.path)  # the next start
    assert libraries.backfill(again) is False
    assert (home / "Games").resolve() not in libraries.library_roots(again)


def test_backfill_still_writes_a_missing_manifest_after_the_first_start(env):
    cfg, home = env
    game_dir = _old_game(cfg, home)
    libraries.backfill(cfg)
    (game_dir / libraries.MANIFEST_NAME).unlink()
    libraries.backfill(cfg)
    assert libraries.read_manifest(game_dir).kind == KIND


def test_an_adopted_default_library_game_keeps_its_folder_name_and_can_be_deleted(env):
    from nadare.core import removal

    cfg, _home = env
    old = paths.profiles_dir() / "brown-dust-2"  # a b22 name: the title slug
    (old / "install").mkdir(parents=True)
    (old / "install" / "game.exe").write_text("x")
    (old / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": KIND,
                "title": "Brown Dust 2",
                "install": "install",
                "made_by_nadare": ["install"],
            }
        )
    )
    found, _notes = libraries.scan(cfg, libraries.default_root())
    assert [c.game_dir for c in found] == [old.resolve()]
    name = libraries.adopt(cfg, found[0])
    assert name == "brown-dust-2"
    trees = {t.key: t for t in removal.removable_trees(cfg, name)}
    assert trees["install_dir"].safe, trees["install_dir"].reason


def test_backfill_repairs_a_damaged_libraries_setting(env):
    """Round 5: "libraries": {} counted as "set up already", so nothing was ever remembered."""
    cfg, home = env
    _old_game(cfg, home)
    cfg.data[libraries.CONFIG_KEY] = {}
    assert libraries.backfill(cfg) is True
    assert (home / "Games").resolve() in libraries.library_roots(cfg)


def test_backfill_repairs_a_libraries_list_with_no_usable_entry(env):
    """Round 6: a hand-edited list of plain paths was "set up" and never repaired."""
    cfg, home = env
    _old_game(cfg, home)
    cfg.data[libraries.CONFIG_KEY] = [str(home / "Games")]
    assert libraries.backfill(cfg) is True
    assert (home / "Games").resolve() in libraries.library_roots(cfg)


def test_an_empty_libraries_list_is_the_users_choice(env):
    cfg, home = env
    _old_game(cfg, home)
    cfg.data[libraries.CONFIG_KEY] = []  # every library removed in Settings
    libraries.backfill(cfg)
    assert (home / "Games").resolve() not in libraries.library_roots(cfg)


def test_an_adopted_default_library_game_gets_its_manifest_back(env):
    """Round 6: the adopted profile's owned_dirs was one the repair path rejects."""
    cfg, _home = env
    old = paths.profiles_dir() / "brown-dust-2"
    (old / "install").mkdir(parents=True)
    (old / "install" / "game.exe").write_text("x")
    manifest = {
        "schema": libraries.MANIFEST_SCHEMA,
        "backend": KIND,
        "title": "Brown Dust 2",
        "install": "install",
        "made_by_nadare": ["install"],
    }
    (old / libraries.MANIFEST_NAME).write_text(json.dumps(manifest))
    found, _notes = libraries.scan(cfg, libraries.default_root())
    name = libraries.adopt(cfg, found[0])
    (old / libraries.MANIFEST_NAME).unlink()
    libraries.backfill(cfg)
    assert libraries.read_manifest(old) is not None
    assert libraries.read_manifest(old).kind == KIND
    assert name == "brown-dust-2"
