# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade tests b23 -> b24: data an older release wrote, opened by this one (DECISIONS 2026-11-13).

Each case is a finding of the b24 upgrade audit (agent notes, 2026-11-13), checked in the code:

- Kuro: b23 wrote ``.nadare/tidied-<version>`` even when its cleanup silently gave up (the support
  case). b24's reliable cleanup sat behind that same mark, so it never ran for those players.
- Hi3: b24 installs Hi3 through Sophon. An install b23 made from the 7z has the archive record but
  no Sophon markers, so "Check game files" reported both packages missing on correct files.
- The "found in another folder" offer (#1293): an unplugged drive, a stale offer, Nadare's own
  default library and a ``~`` path must never re-point a game that is fine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import gamefinder, kuropatch, libraries
from nadare.core.config import Config
from nadare.core.relink import RelinkError
from nadare.core.sources import hoyo

# ---- Kuro: a b23 cleanup mark ---------------------------------------------------------------

ROOT = "https://cdn.example/"
LISTS = {f"{ROOT}p/3.7.2.json": {"deleteFiles": ["Client/Content/Paks/old-a.pak"]}}
EXTRA = {kuropatch.EXTRA_KEY: {"root": ROOT, "packs": {"common": [
    {"version": "3.7.2", "indexFile": "p/3.7.2.json", "baseUrl": "x/"},
]}}}
RETIRED = "Client/Content/Paks/old-a.pak"


@pytest.fixture()
def wuwa(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "pfx")})
    backend = direct.WutheringWavesBackend(ctx)
    root = backend.install_dir()
    (root / RETIRED).parent.mkdir(parents=True, exist_ok=True)
    (root / RETIRED).write_bytes(b"x")
    target = backend.direct_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    backend._write_version_file("3.8.0", "wuwa")
    exe = dd.RemoteFile(target.relative_to(root).as_posix(), "https://x/exe", 2, "0" * 32)
    plan = dd.Plan(game_tag="wuwa", version="3.8.0", files=(exe,), extra=EXTRA)
    backend._load_manifest = lambda: plan
    calls: list[str] = []

    def fetch(url, headers=None):
        calls.append(url)
        return json.dumps(LISTS[url]).encode()

    monkeypatch.setattr(direct, "patch_list_fetch", fetch)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "info", lambda text: None)
    return backend, root, calls


def test_a_cleanup_mark_written_by_b23_does_not_stop_the_new_cleanup(wuwa):
    backend, root, _calls = wuwa
    mark = root / dd.STATE_DIR / "tidied-3.8.0"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text("", encoding="utf-8")  # exactly what b23 wrote, success or not
    backend._tidy_retired_once()
    assert not (root / RETIRED).exists()
    assert mark.exists()


def test_a_mark_written_by_this_release_is_trusted(wuwa):
    backend, root, calls = wuwa
    backend._tidy_retired_once()
    assert not (root / RETIRED).exists()
    (root / RETIRED).write_bytes(b"x")  # e.g. a mod tool put it back: not our business again
    before = len(calls)
    backend._tidy_retired_once()
    assert len(calls) == before  # no second scan for the same version
    assert (root / RETIRED).exists()


# ---- Hi3: an install b23 made from the 7z -------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"


def _hi3(tmp_path, monkeypatch):
    from test_hi3_sophon import _fetch  # the live-reply fixtures of 2026-11-12

    monkeypatch.setattr(hoyo, "default_fetch", _fetch())
    backend = direct.Hi3Backend(
        make_context(tmp_path, "direct-hi3", {"prefix": str(tmp_path / "pfx")})
    )
    write_exe(backend.direct_target(), "MZ")
    return backend, backend.install_dir()


def test_check_game_files_on_a_b23_hi3_install_reports_no_missing_package(tmp_path, monkeypatch):
    backend, root = _hi3(tmp_path, monkeypatch)
    state = root / dd.STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "hi3-9.1.0.pack.done").write_text(json.dumps({
        "version": "9.1.0", "game": "hi3", "slot": None,
        "url": "https://autopatchglb.honkaiimpact3.com/x/BH3_v9.1.0.7z",
        "members": ["BH3.exe"],
    }))
    names = [getattr(e, "path", str(e)) for e in backend._missing_entries()]
    assert not [n for n in names if n.startswith("hi3-")], names


def test_a_hi3_install_with_neither_record_still_reports_missing_packages(tmp_path, monkeypatch):
    """The guard above must not hide a real gap: no archive record, no Sophon marker."""
    backend, _root = _hi3(tmp_path, monkeypatch)
    names = [getattr(e, "path", str(e)) for e in backend._missing_entries()]
    assert {"hi3-9.1.0", "hi3-9.1.0-asb"} <= set(names)


# ---- the found-copy offer ---------------------------------------------------------------------

HSR = libraries.GameSpec(
    kind="direct-hsr", title="Honkai: Star Rail", exes=("StarRail.exe",),
    install_root="drive_c/Games", install_subdir="HonkaiStarRail",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), tmp_path, home


def _copy(game_dir: Path) -> Path:
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "StarRail.exe").write_text("x")
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(json.dumps({
        "schema": libraries.MANIFEST_SCHEMA, "backend": "direct-hsr",
        "title": "Honkai: Star Rail", "install": "install", "prefix": "prefix",
        "made_by_nadare": ["install", "prefix"],
    }))
    return game_dir.resolve()


def _profile(cfg, game_dir: Path):
    cfg.add_profile(
        "hsr", backend="direct-hsr", title="Honkai: Star Rail",
        install_dir=str(game_dir / "install"), prefix=str(game_dir / "prefix"),
        owned_dirs=[str(game_dir)], owned_trees=["install_dir", "prefix"],
    )


def test_a_game_on_an_unplugged_drive_is_not_offered_another_copy(env):
    """Its saves live in its prefix: switching to another copy would look like losing them."""
    cfg, tmp, _home = env
    lib = tmp / "local"
    _copy(lib / "Nadare" / "Honkai Star Rail")
    usb = tmp / "media" / "usb" / "Games"  # the drive is not mounted: nothing of it exists
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib)}, {"path": str(usb)}]
    _profile(cfg, usb / "Nadare" / "Honkai Star Rail")
    assert gamefinder.repairs(cfg, [HSR]) == {}


def test_the_support_case_is_still_offered(env):
    """The library folder exists; only the game's own folder layout differs."""
    cfg, tmp, _home = env
    lib = tmp / "Gacha games"
    real = _copy(lib / "Nadare" / "Honkai Star Rail")
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib)}]
    _profile(cfg, lib / "Nadare" / "Nadare" / "Honkai Star Rail")
    (copy,) = gamefinder.repairs(cfg, [HSR])["hsr"]
    assert copy.game_dir == real


def test_use_refuses_once_the_game_is_back_where_the_profile_says(env):
    """The offer was computed at start; the drive came back or the game was reinstalled since."""
    cfg, tmp, _home = env
    lib = tmp / "Gacha games"
    _copy(lib / "Nadare" / "Honkai Star Rail")
    old = lib / "Nadare" / "Nadare" / "Honkai Star Rail"
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib)}]
    _profile(cfg, old)
    (copy,) = gamefinder.repairs(cfg, [HSR])["hsr"]
    _copy(old)  # the recorded folder is there again
    before = json.dumps(cfg.data, sort_keys=True)
    with pytest.raises(RelinkError):
        gamefinder.use(cfg, "hsr", copy)
    assert json.dumps(cfg.data, sort_keys=True) == before


def test_nadares_default_library_is_not_searched_for_copies(env):
    """Its folders are named after profiles (``profiles/<name>``), not games: a leftover there is
    another profile's folder, and claiming it would give a profile a folder it cannot keep."""
    cfg, _tmp, _home = env
    _copy(libraries.default_root() / "hsr-old")
    cfg.add_profile(
        "hsr", backend="direct-hsr", title="Honkai: Star Rail",
        install_dir=str(libraries.default_root() / "hsr" / "install"),
        prefix=str(libraries.default_root() / "hsr" / "prefix"),
        owned_trees=["install_dir", "prefix"],
    )
    assert gamefinder.repairs(cfg, [HSR]) == {}


def test_a_tilde_path_that_exists_is_not_missing(env):
    _cfg, _tmp, home = env
    (home / "Games" / "Nadare" / "HSR" / "install").mkdir(parents=True)
    profile = {
        "backend": "direct-hsr",
        "install_dir": "~/Games/Nadare/HSR/install",
        "prefix": "~/Games/Nadare/HSR/install",
    }
    assert gamefinder.folders_missing(profile) == ()


def test_after_the_first_sophon_sync_a_missing_package_counts_again(tmp_path, monkeypatch):
    """The old archive record stays on disk for good: it may excuse missing Sophon markers only
    until Sophon has run once for this game (a marker exists)."""
    backend, root = _hi3(tmp_path, monkeypatch)
    state = root / dd.STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "hi3-9.1.0.pack.done").write_text(json.dumps({
        "version": "9.1.0", "game": "hi3", "slot": None,
        "url": "https://autopatchglb.honkaiimpact3.com/x/BH3_v9.1.0.7z",
        "members": ["BH3.exe"],
    }))
    plan = backend.plan()
    game = next(p for p in plan.sophon if p.name == "hi3-9.1.0")
    (state / f"sophon-{game.name}.done").write_text(game.manifest_id, encoding="utf-8")
    names = [getattr(e, "path", str(e)) for e in backend._missing_entries()]
    assert "hi3-9.1.0-asb" in names
    assert "hi3-9.1.0" not in names
