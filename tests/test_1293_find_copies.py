# SPDX-License-Identifier: GPL-3.0-or-later
"""#1293: a game's files are on disk but its profile points at a folder that is not there.

b21 put a game picked into a folder named ``Nadare`` at ``X/Nadare/Nadare/<Game>``; b22 changed
what that library entry means and put new games at ``X/Nadare/<Game>``. A profile written by one
rule and files laid out by the other showed "Not installed" with no way back in the app. These
are UPGRADE tests: config and folders as b21/b22 left them, opened by the new code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core import gamefinder, libraries
from nadare.core.config import Config
from nadare.core.relink import RelinkError

HSR = libraries.GameSpec(
    kind="direct-hsr",
    title="Honkai: Star Rail",
    exes=("StarRail.exe",),
    install_root="drive_c/Games",
    install_subdir="HonkaiStarRail",
)
WUWA = libraries.GameSpec(
    kind="direct-wuwa",
    title="Wuthering Waves",
    exes=("Wuthering Waves.exe",),
    install_root="drive_c/Games",
    install_subdir="WutheringWaves",
)
NIKKE = libraries.GameSpec(
    kind="launcher-nikke",
    title="NIKKE: Goddess of Victory",
    exes=("nikke_launcher.exe",),
    install_root="drive_c",
    install_subdir="NIKKE",
)
SPECS = [HSR, WUWA, NIKKE]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    drive = tmp_path / "drive" / "Gacha games"
    drive.mkdir(parents=True)
    return Config.load(tmp_path / "config.json"), drive


def _game_tree(
    game_dir: Path,
    spec: libraries.GameSpec,
    *,
    install: bool = True,
    prefix: bool = True,
    manifest: bool = True,
    version: str | None = None,
    incomplete: bool = False,
) -> Path:
    """A game folder the way Add Game lays it out, with what an install leaves there."""
    game_dir.mkdir(parents=True, exist_ok=True)
    made = []
    data = {"schema": libraries.MANIFEST_SCHEMA, "backend": spec.kind, "title": spec.title}
    if install:
        (game_dir / "install").mkdir()
        (game_dir / "install" / spec.exes[0]).write_text("exe")
        if version:
            (game_dir / "install" / ".nadare-version.json").write_text(
                json.dumps({"version": version})
            )
        if incomplete:
            (game_dir / "install" / ".nadare-install-incomplete").write_text("x\n")
        data["install"] = "install"
        made.append("install")
    if prefix:
        (game_dir / "prefix" / "drive_c" / "windows").mkdir(parents=True)
        data["prefix"] = "prefix"
        made.append("prefix")
    data["made_by_nadare"] = made
    if manifest:
        (game_dir / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    return game_dir


def _nikke_tree(game_dir: Path) -> Path:
    exe = game_dir / "prefix" / "drive_c" / "NIKKE" / "nikke_launcher.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("exe")
    data = {
        "schema": libraries.MANIFEST_SCHEMA,
        "backend": NIKKE.kind,
        "title": NIKKE.title,
        "prefix": "prefix",
        "made_by_nadare": ["prefix"],
    }
    (game_dir / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    return game_dir


def _profile(cfg: Config, name: str, spec: libraries.GameSpec, game_dir: Path, *, install=True):
    """A profile as Add Game wrote it for ``game_dir`` (which need not exist)."""
    values = {
        "backend": spec.kind,
        "title": spec.title,
        "prefix": str(game_dir / "prefix"),
        "installed_prefix": str(game_dir / "prefix"),
        "owned_dirs": [str(game_dir)],
        "owned_trees": ["install_dir", "prefix"] if install else ["prefix"],
    }
    if install:
        values["install_dir"] = str(game_dir / "install")
    values["owned_paths"] = {key: str(Path(values[key])) for key in values["owned_trees"]}
    cfg.add_profile(name, **values)
    return cfg.profiles[name]


def _set_libraries(cfg: Config, *roots: Path) -> None:
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(root)} for root in roots]


def _snapshot(root: Path) -> dict[str, tuple]:
    """Every path under ``root`` with its type, size and mtime."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for item in dirnames + filenames:
            p = Path(dirpath) / item
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def _support_case(cfg: Config, drive: Path, library_entries: tuple[Path, ...]):
    """The 2026-11-13 support case: WuWa (b21 layout, profile right), HSR (b22 layout, profile
    pointing at the b21 layout, which does not exist)."""
    _set_libraries(cfg, *library_entries)
    wuwa_dir = _game_tree(drive / "Nadare" / "Nadare" / "Wuthering Waves", WUWA, version="3.8.0")
    _profile(cfg, "wuwa", WUWA, wuwa_dir)
    hsr_real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, version="3.7.0")
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    return wuwa_dir, hsr_real


LIBRARY_VIEWS = {
    "b21-entry-is-the-Nadare-folder": lambda d: (d / "Nadare",),
    "b22-entry-is-its-parent": lambda d: (d,),
    "both-entries": lambda d: (d, d / "Nadare"),
    "no-entries": lambda d: (),
}


# ---- which profiles need a repair -----------------------------------------------------------


@pytest.mark.parametrize("view", LIBRARY_VIEWS)
def test_the_support_case_is_found_and_only_the_broken_game_is_reported(env, view):
    cfg, drive = env
    _wuwa, hsr_real = _support_case(cfg, drive, LIBRARY_VIEWS[view](drive))
    assert gamefinder.folders_missing(cfg.profiles["hsr"])
    assert not gamefinder.folders_missing(cfg.profiles["wuwa"])
    found = gamefinder.repairs(cfg, SPECS)
    assert list(found) == ["hsr"]
    best = found["hsr"][0]
    assert best.game_dir == hsr_real.resolve()
    assert best.install_dir == (hsr_real / "install").resolve()
    assert best.prefix == (hsr_real / "prefix").resolve()
    assert best.complete
    assert best.version == "3.7.0"
    assert best.from_manifest
    assert not best.recorded


def test_the_other_direction_is_found_too(env):
    """Profile written by b22 (flat), files left by b21 (Nadare/Nadare)."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [real.resolve()]


def test_a_game_folder_named_differently_is_found_by_its_manifest(env):
    """The title changed since the install, or the user renamed the folder: the manifest says
    which game it is, not the folder name."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "HSR old", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    assert [c.game_dir for c in gamefinder.find_copies(cfg, "hsr", HSR)] == [real.resolve()]


def test_a_working_game_is_never_reported_even_with_another_copy_on_disk(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    used = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, used)
    assert not gamefinder.folders_missing(cfg.profiles["hsr"])
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_a_deleted_game_with_no_copy_anywhere_is_not_reported(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Honkai Star Rail")
    assert gamefinder.folders_missing(cfg.profiles["hsr"])
    assert gamefinder.find_copies(cfg, "hsr", HSR) == []
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_a_deleted_prefix_alone_is_not_a_repair(env):
    """The game files are where the profile says; only the prefix is gone (the player deleted
    it, Nadare makes a new one at the next launch). The game is installed: no banner, ever."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    game = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    # Another copy exists too: it must not be offered for a game that works.
    _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, game)
    for path in sorted((game / "prefix").rglob("*"), reverse=True):
        path.rmdir()
    (game / "prefix").rmdir()
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_the_profiles_own_folder_is_never_offered_back(env):
    """A NIKKE profile whose prefix is gone: its own (now empty) game folder is not a copy."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _nikke_tree(drive / "Nadare" / "NIKKE Goddess of Victory")
    _profile(cfg, "nikke", NIKKE, real, install=False)
    exe = real / "prefix" / "drive_c" / "NIKKE" / "nikke_launcher.exe"
    exe.unlink()
    for path in sorted((real / "prefix").rglob("*"), reverse=True):
        path.rmdir()
    (real / "prefix").rmdir()
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_a_custom_wine_game_is_left_alone(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    cfg.add_profile("mine", backend="wine", title="Mine", prefix=str(drive / "gone" / "prefix"))
    assert not gamefinder.folders_missing(cfg.profiles["mine"])
    assert gamefinder.find_copies(cfg, "mine", None) == []
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_a_default_library_game_without_recorded_folders_is_not_missing(env):
    """A game in ``profiles/<name>`` records no folders; nothing is "missing" there."""
    cfg, _drive = env
    cfg.add_profile("hsr", backend="direct-hsr", title="Honkai: Star Rail")
    assert not gamefinder.folders_missing(cfg.profiles["hsr"])


# ---- what counts as a copy ------------------------------------------------------------------


def test_two_copies_are_listed_best_first(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    half = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, version="3.7.0", incomplete=True)
    whole = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR, version="3.6.0")
    _profile(cfg, "hsr", HSR, drive / "elsewhere" / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [whole.resolve(), half.resolve()]
    assert copies[0].complete and not copies[1].complete
    # The list arrives best first whatever order the folders are read in.
    assert gamefinder.best(list(reversed(copies))) == copies[0]


def test_a_complete_copy_beats_an_incomplete_one_with_a_version(env):
    """A finished install with no version record is worth more than half a newer one."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    half = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, version="3.7.0", incomplete=True)
    whole = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [whole.resolve(), half.resolve()]


def test_between_complete_copies_the_newer_version_wins_by_number_not_by_text(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    older = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, version="3.9.0")
    newer = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR, version="3.10.0")
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [newer.resolve(), older.resolve()]


def test_a_recorded_version_beats_none(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    unknown = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    known = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR, version="1.0.0")
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [known.resolve(), unknown.resolve()]


def test_a_prefix_only_leftover_is_listed_as_incomplete_below_the_real_game(env):
    """The "fake" folder: a prefix and a manifest, but the game files are not there."""
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    fake = drive / "Nadare" / "Honkai Star Rail"
    _game_tree(fake, HSR)
    for item in (fake / "install").iterdir():
        item.unlink()
    (fake / "install").rmdir()
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    copies = gamefinder.find_copies(cfg, "hsr", HSR)
    assert [c.game_dir for c in copies] == [real.resolve(), fake.resolve()]
    assert not copies[1].complete


def test_a_prefix_without_drive_c_is_not_complete(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    game = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, prefix=False)
    (game / "prefix").mkdir()
    data = json.loads((game / libraries.MANIFEST_NAME).read_text())
    data["prefix"] = "prefix"
    (game / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    assert not copy.complete


def test_another_games_folder_is_not_a_copy(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    _game_tree(drive / "Nadare" / "Honkai Star Rail", WUWA)  # wrong game, right folder name
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    assert gamefinder.find_copies(cfg, "hsr", HSR) == []


def test_a_damaged_manifest_is_not_guessed_over(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    game = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    (game / libraries.MANIFEST_NAME).write_text("{not json")
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    assert gamefinder.find_copies(cfg, "hsr", HSR) == []


def test_a_copy_another_profile_uses_is_not_offered(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    theirs = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    # A duplicate profile from before "one profile per game" uses that copy.
    _profile(cfg, "hsr-2", HSR, theirs)
    assert gamefinder.find_copies(cfg, "hsr", HSR) == []


def test_a_symlinked_game_folder_is_not_a_copy(env, tmp_path):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(tmp_path / "elsewhere" / "Honkai Star Rail", HSR)
    (drive / "Nadare").mkdir()
    (drive / "Nadare" / "Honkai Star Rail").symlink_to(real, target_is_directory=True)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    assert gamefinder.find_copies(cfg, "hsr", HSR) == []


def test_a_folder_without_a_manifest_is_found_by_the_game_program(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    game = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, manifest=False)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    assert copy.game_dir == game.resolve()
    assert not copy.from_manifest
    assert copy.made_by_nadare == ()


def test_without_a_spec_only_manifests_are_used(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, manifest=False)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    assert gamefinder.find_copies(cfg, "hsr", None) == []


def test_nikke_a_prefix_only_game_is_found(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _nikke_tree(drive / "Nadare" / "NIKKE Goddess of Victory")
    _profile(cfg, "nikke", NIKKE, drive / "Nadare" / "Nadare" / "NIKKE Goddess of Victory",
             install=False)
    found = gamefinder.repairs(cfg, SPECS)
    (copy,) = found["nikke"]
    assert copy.game_dir == real.resolve()
    assert copy.install_dir is None
    assert copy.prefix == (real / "prefix").resolve()
    assert copy.complete


def test_finding_changes_nothing_on_disk_or_in_the_config(env):
    cfg, drive = env
    _support_case(cfg, drive, (drive / "Nadare",))
    before_disk = _snapshot(drive.parent)
    before_cfg = json.dumps(cfg.data, sort_keys=True)
    gamefinder.repairs(cfg, SPECS)
    gamefinder.find_copies(cfg, "hsr", HSR)
    assert _snapshot(drive.parent) == before_disk
    assert json.dumps(cfg.data, sort_keys=True) == before_cfg


# ---- [Use this folder] ----------------------------------------------------------------------


@pytest.mark.parametrize("view", LIBRARY_VIEWS)
def test_use_this_folder_repoints_the_profile_and_touches_no_game_file(env, view):
    cfg, drive = env
    _wuwa, hsr_real = _support_case(cfg, drive, LIBRARY_VIEWS[view](drive))
    files_before = {
        k: v for k, v in _snapshot(drive.parent).items() if not k.endswith(libraries.MANIFEST_NAME)
    }
    wuwa_before = dict(cfg.profiles["wuwa"])
    copy = gamefinder.repairs(cfg, SPECS)["hsr"][0]
    gamefinder.use(cfg, "hsr", copy)
    prof = cfg.profiles["hsr"]
    real = hsr_real.resolve()
    assert Path(prof["install_dir"]) == real / "install"
    assert Path(prof["prefix"]) == real / "prefix"
    assert prof["owned_dirs"] == [str(real)]
    assert sorted(prof["owned_trees"]) == ["install_dir", "prefix"]
    assert prof["owned_paths"] == {
        "install_dir": str(real / "install"),
        "prefix": str(real / "prefix"),
    }
    assert Path(prof["installed_prefix"]) == real / "prefix"
    # Nothing was created, moved or deleted, and no other game changed.
    files_after = {
        k: v for k, v in _snapshot(drive.parent).items() if not k.endswith(libraries.MANIFEST_NAME)
    }
    assert files_after == files_before
    assert not (drive / "Nadare" / "Nadare" / "Honkai Star Rail").exists()
    assert cfg.profiles["wuwa"] == wuwa_before
    # The manifest in the chosen folder matches the profile.
    manifest = libraries.read_manifest(real)
    assert manifest is not None and manifest.kind == "direct-hsr"
    assert manifest.install_dir == real / "install" and manifest.prefix == real / "prefix"
    assert not gamefinder.folders_missing(prof)
    assert gamefinder.repairs(cfg, SPECS) == {}


def test_the_next_start_keeps_the_repair(env):
    """``backfill`` runs on every start: it must not undo the repair or make the old folder."""
    cfg, drive = env
    _support_case(cfg, drive, (drive / "Nadare",))
    gamefinder.use(cfg, "hsr", gamefinder.repairs(cfg, SPECS)["hsr"][0])
    after = json.loads(json.dumps(cfg.profiles["hsr"]))
    libraries.backfill(cfg)
    assert cfg.profiles["hsr"] == after
    assert not (drive / "Nadare" / "Nadare" / "Honkai Star Rail").exists()


def test_the_start_before_a_repair_creates_no_folder(env):
    cfg, drive = env
    _support_case(cfg, drive, (drive / "Nadare",))
    before = _snapshot(drive.parent)
    libraries.backfill(cfg)
    assert _snapshot(drive.parent) == before


def test_use_remembers_the_library_the_way_add_game_names_it(env):
    """A copy at ``X/Nadare/<Game>`` is in the library ``X`` (never the ``Nadare`` folder)."""
    cfg, drive = env
    _support_case(cfg, drive, ())
    gamefinder.use(cfg, "hsr", gamefinder.repairs(cfg, SPECS)["hsr"][0])
    roots = libraries.library_roots(cfg)
    assert drive.resolve() in roots
    assert (drive / "Nadare").resolve() not in roots


def test_use_does_not_fail_when_the_library_cannot_be_added(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    gamefinder.use(cfg, "hsr", copy)  # X/Nadare cannot be a library while X is one
    assert Path(cfg.profiles["hsr"]["install_dir"]) == (real / "install").resolve()


def test_use_claims_only_what_the_manifest_says_nadare_made(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    data = json.loads((real / libraries.MANIFEST_NAME).read_text())
    data["made_by_nadare"] = ["install"]
    (real / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    gamefinder.use(cfg, "hsr", copy)
    prof = cfg.profiles["hsr"]
    assert prof["owned_trees"] == ["install_dir"]
    assert set(prof["owned_paths"]) == {"install_dir"}
    # The uninstall guard's record moves only with a prefix Nadare made.
    assert Path(prof["installed_prefix"]) != (real / "prefix").resolve()


def test_use_of_a_guessed_folder_claims_nothing_and_writes_no_manifest(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, manifest=False)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    gamefinder.use(cfg, "hsr", copy)
    prof = cfg.profiles["hsr"]
    assert Path(prof["install_dir"]) == (real / "install").resolve()
    assert Path(prof["prefix"]) == (real / "prefix").resolve()
    assert not prof.get("owned_trees")
    assert not prof.get("owned_paths")
    assert not prof.get("owned_dirs")
    assert not (real / libraries.MANIFEST_NAME).exists()


def test_use_for_nikke_sets_only_the_prefix(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _nikke_tree(drive / "Nadare" / "NIKKE Goddess of Victory")
    _profile(cfg, "nikke", NIKKE, drive / "Nadare" / "Nadare" / "NIKKE Goddess of Victory",
             install=False)
    (copy,) = gamefinder.find_copies(cfg, "nikke", NIKKE)
    gamefinder.use(cfg, "nikke", copy)
    prof = cfg.profiles["nikke"]
    assert Path(prof["prefix"]) == (real / "prefix").resolve()
    assert not prof.get("install_dir")
    assert prof["owned_trees"] == ["prefix"]


def test_use_refuses_a_copy_of_another_game(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    wuwa = _game_tree(drive / "Nadare" / "Wuthering Waves", WUWA)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    _profile(cfg, "wuwa", WUWA, drive / "Nadare" / "Nadare" / "Wuthering Waves")
    (copy,) = gamefinder.find_copies(cfg, "wuwa", WUWA)
    assert copy.game_dir == wuwa.resolve()
    before = json.dumps(cfg.data, sort_keys=True)
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "hsr", copy)
    assert json.dumps(cfg.data, sort_keys=True) == before


def test_use_refuses_a_copy_another_profile_took_meanwhile(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    _profile(cfg, "hsr-2", HSR, real)
    before = json.dumps(cfg.data, sort_keys=True)
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "hsr", copy)
    assert json.dumps(cfg.data, sort_keys=True) == before


def test_use_refuses_a_prefix_holding_files_but_no_drive_c(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    game = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR, prefix=False)
    (game / "prefix").mkdir()
    (game / "prefix" / "something.txt").write_text("not a prefix")
    data = json.loads((game / libraries.MANIFEST_NAME).read_text())
    data["prefix"] = "prefix"
    (game / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    _profile(cfg, "hsr", HSR, drive / "x" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "hsr", copy)


def test_use_refuses_a_folder_that_was_removed_since_it_was_found(env):
    cfg, drive = env
    _set_libraries(cfg, drive)
    real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    _profile(cfg, "hsr", HSR, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.find_copies(cfg, "hsr", HSR)
    real.rename(drive / "renamed")
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "hsr", copy)


def test_use_refuses_a_custom_wine_game(env):
    cfg, drive = env
    real = _game_tree(drive / "Nadare" / "Honkai Star Rail", HSR)
    cfg.add_profile("mine", backend="wine", title="Mine", prefix=str(drive / "gone"))
    copy = gamefinder.Copy(
        game_dir=real.resolve(),
        library=drive.resolve(),
        install_dir=(real / "install").resolve(),
        prefix=(real / "prefix").resolve(),
        made_by_nadare=("install", "prefix"),
        version=None,
        complete=True,
        recorded=False,
        from_manifest=True,
        kind="direct-hsr",
    )
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "mine", copy)


def test_after_use_the_game_shows_installed(env):
    """End to end through the real backend: the page stops saying "Not installed"."""
    from nadare.core.backends import create

    cfg, drive = env
    _support_case(cfg, drive, (drive / "Nadare",))
    assert create("hsr", cfg).status().state == "not_installed"
    gamefinder.use(cfg, "hsr", gamefinder.repairs(cfg, SPECS)["hsr"][0])
    assert create("hsr", cfg).status().state != "not_installed"
