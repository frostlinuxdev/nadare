# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1591 item 1: the launcher web caches on the Disk space card.

A game's Wine prefix may hold an embedded Chromium/CEF/WebView2 profile (the
NIKKE launcher, Snowbreak, CZN).  The card lists one row per game, sizes only the
cache folders Nadare may clear, and never touches the files that keep the player
signed in.  Everything is measured in a throw-away HOME/prefix.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from nadare.core import storagecard

DAY = 86400
#: The only folders a Clear may empty (the brief's list).
ALLOWED = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "Service Worker/CacheStorage",
    "Service Worker/ScriptCache",
)
#: Everything that must survive: it holds the player's sign-in or settings.
NEVER_TOUCH = (
    ("Cookies", False),
    ("Cookies-journal", False),
    ("Local Storage", True),
    ("Session Storage", True),
    ("IndexedDB", True),
    ("databases", True),
    ("Local State", False),
    ("Preferences", False),
    ("LocalPrefs.json", False),
    ("Network Persistent State", False),
    ("TransportSecurity", False),
    ("LOCK", False),
    ("CURRENT", False),
    ("notes.txt", False),
)


def _file(path: Path, size: int = 10, age: float = 2 * DAY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def _dir(path: Path, files: int = 1, size: int = 10) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        _file(path / f"f{i}", size)
    return path


def _appdata(prefix: Path, area: str = "Local", user: str = "steamuser") -> Path:
    return prefix / "drive_c" / "users" / user / "AppData" / area


def _nikke_profile(prefix: Path) -> Path:
    """The measured NIKKE launcher layout (Roaming/nikke_launcher/tbs_cache)."""
    profile = _appdata(prefix, "Roaming") / "nikke_launcher" / "tbs_cache"
    _dir(profile / "Cache", size=10)
    _dir(profile / "Code Cache", size=20)
    _dir(profile / "GPUCache", size=30)
    _dir(profile / "Service Worker" / "CacheStorage", size=5)
    _dir(profile / "Service Worker" / "ScriptCache", size=7)
    _dir(profile / "Local Storage", size=3)
    _dir(profile / "IndexedDB", size=4)
    for name in (
        "Cookies",
        "LOCK",
        "CURRENT",
        "Network Persistent State",
        "LocalPrefs.json",
    ):
        _file(profile / name, size=0)
    return profile


def _snowbreak_profile(prefix: Path) -> Path:
    """The measured Snowbreak layout (Local/CEF/User Data)."""
    profile = _appdata(prefix) / "CEF" / "User Data"
    _dir(profile / "Cache", size=40)
    _file(profile / "Cookies", size=0)
    return profile


def _czn_profile(prefix: Path) -> Path:
    """The measured WebView2 layout (Local/<app>/EBWebView/Default)."""
    profile = _appdata(prefix) / "CZN" / "EBWebView" / "Default"
    _dir(profile / "Cache", size=50)
    _file(profile / "Preferences", size=0)
    return profile


def _profile(prefix: Path, area: str = "Local", *parts: str) -> Path:
    """A minimal profile that passes detection, with its ``Cache`` holding one file."""
    folder = _appdata(prefix, area).joinpath(*parts)
    _dir(folder / "Cache", size=10)
    _file(folder / "Cookies", size=0)
    return folder


def _config(*profiles: tuple[str, dict]) -> dict:
    return {name: profile for name, profile in profiles}


def _scan(profiles: dict, *, running=lambda _prefix: False) -> storagecard.Report:
    return storagecard.scan(profiles, steamapps=[], running=running)


def _game(prefix: Path, title: str = "Game") -> dict:
    return {"backend": "wine", "title": title, "prefix": str(prefix)}


def test_scan_finds_the_three_measured_layouts_and_sizes_only_their_caches(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "prefix"
    nikke = _nikke_profile(prefix)
    snowbreak = _snowbreak_profile(prefix)
    czn = _czn_profile(prefix)
    # Neither folder is a browser profile: one has a cache but no sign-in marker,
    # the other a marker but no cache.
    _dir(_appdata(prefix) / "NoMarker" / "Cache", size=99)
    _file(_appdata(prefix) / "NoCache" / "Cookies", size=0)

    report = _scan(_config(("nikke", _game(prefix, "NIKKE"))))

    assert len(report.webcaches) == 1
    row = report.webcaches[0]
    assert row.profile == "nikke"
    assert row.title == "NIKKE"
    assert row.prefix == prefix
    assert set(row.profiles) == {nikke, snowbreak, czn}
    # 10 + 20 + 30 + 5 + 7 + 40 + 50; the Local Storage and IndexedDB files do not count.
    assert row.size == 162
    assert row.running is False


def test_a_game_without_a_browser_profile_is_not_listed(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    _dir(prefix / "drive_c/users/steamuser/AppData/Local/SomeGame/data", size=10)
    report = _scan(_config(("g", _game(prefix))))
    assert report.webcaches == ()


def test_a_profile_below_the_depth_cap_is_ignored(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    shallow = _profile(prefix, "Local", "a", "b", "c", "d", "profile")  # depth 5
    _profile(prefix, "Local", "a", "b", "c", "d", "e", "profile")  # depth 6

    report = _scan(_config(("g", _game(prefix))))

    assert len(report.webcaches) == 1
    assert report.webcaches[0].profiles == (shallow,)


def test_a_profile_in_users_public_is_ignored(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    _profile(prefix, "Local", "game")  # a real one, so the prefix is listed

    # Build Public directly: _appdata() would join through a literal "Public" user.
    public = prefix / "drive_c/users/Public/AppData/Local/PublicGame"
    _dir(public / "Cache", size=99)
    _file(public / "Cookies", size=0)

    report = _scan(_config(("g", _game(prefix))))

    assert len(report.webcaches) == 1
    assert public not in report.webcaches[0].profiles


def test_the_directory_cap_stops_the_walk(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    local = _appdata(prefix)
    local.mkdir(parents=True)
    for i in range(2001):
        (local / f"d{i:04d}").mkdir()
    _profile(prefix, "Local", "zzz", "profile")

    report = _scan(_config(("g", _game(prefix))))

    assert report.webcaches == ()


def test_clear_empty_and_recreates_only_the_allowed_folders(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    profile = _appdata(prefix) / "CEF" / "User Data"
    allowed_paths = []
    for name in ALLOWED:
        folder = profile / name
        _dir(folder, files=2, size=7)
        allowed_paths.append(folder)
    kept_paths = []
    for name, is_dir in NEVER_TOUCH:
        path = profile / name
        if is_dir:
            _dir(path, files=1, size=11)
        else:
            _file(path, size=11)
        kept_paths.append(path)

    report = _scan(_config(("g", _game(prefix))))
    result = storagecard.clear_webcache(report.webcaches[0])

    assert result.kept == []
    assert set(result.removed) == set(allowed_paths)
    assert result.freed == len(ALLOWED) * 2 * 7
    for folder in allowed_paths:
        assert folder.is_dir(), f"{folder} was not recreated"
        assert list(folder.iterdir()) == [], f"{folder} was not emptied"
    for path in kept_paths:
        assert path.exists(), f"{path} was deleted"
        if path.is_file():
            assert path.read_bytes() == b"x" * 11, f"{path} changed"
        else:
            assert list(path.iterdir()), f"{path} was emptied"


def test_a_linked_cache_folder_is_not_followed_and_its_target_survives(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "prefix"
    profile = _appdata(prefix) / "CEF" / "User Data"
    _dir(profile / "GPUCache", size=10)
    _file(profile / "Cookies", size=0)
    outside = _dir(tmp_path / "outside", files=1, size=99)
    linked = profile / "Cache"
    linked.symlink_to(outside, target_is_directory=True)

    report = _scan(_config(("g", _game(prefix))))
    assert len(report.webcaches) == 1
    assert report.webcaches[0].size == 10  # the real GPUCache, not the link's target

    result = storagecard.clear_webcache(report.webcaches[0])

    assert linked in result.kept
    assert linked.is_symlink()
    assert (outside / "f0").read_bytes() == b"x" * 99


def test_a_linked_service_worker_folder_is_not_followed(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    profile = _appdata(prefix) / "CEF" / "User Data"
    profile.mkdir(parents=True)
    _file(profile / "Cookies", size=0)
    outside = tmp_path / "outside"
    _dir(outside / "CacheStorage", size=99)
    (profile / "Service Worker").symlink_to(outside, target_is_directory=True)

    report = _scan(_config(("g", _game(prefix))))

    assert report.webcaches == ()


def test_a_linked_profile_folder_is_never_detected(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    outside = _appdata(tmp_path / "other") / "game"
    _dir(outside / "Cache", size=99)
    _file(outside / "Cookies", size=0)
    link = _appdata(prefix) / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    report = _scan(_config(("g", _game(prefix))))

    assert report.webcaches == ()


def test_a_profile_from_another_games_prefix_is_refused(tmp_path: Path) -> None:
    prefix_a = tmp_path / "a"
    prefix_b = tmp_path / "b"
    profile_b = _profile(prefix_b, "Local", "CEF", "User Data")
    entry = storagecard.WebCache(
        profile="a",
        title="A",
        prefix=prefix_a,
        profiles=(profile_b,),
        size=10,
        running=False,
    )

    result = storagecard.clear_webcache(entry)

    assert result.removed == []
    assert result.kept == [profile_b]
    assert "this game" in result.reason
    assert list((profile_b / "Cache").iterdir()), "another game's web cache was emptied"


def test_clear_refuses_while_the_game_is_running(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    profile = _profile(prefix, "Local", "CEF", "User Data")
    report = _scan(_config(("g", _game(prefix))), running=lambda _prefix: True)
    assert report.webcaches[0].running is True

    result = storagecard.clear_webcache(report.webcaches[0])

    assert result.freed == 0
    assert set(result.kept) == {profile / "Cache"}
    assert "running" in result.reason
    assert list((profile / "Cache").iterdir())


def test_clear_checks_again_even_when_the_row_said_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    profile = _profile(prefix, "Local", "CEF", "User Data")
    report = _scan(_config(("g", _game(prefix))))
    entry = replace(report.webcaches[0], running=False)
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: True)

    result = storagecard.clear_webcache(entry)

    assert result.removed == []
    assert set(result.kept) == {profile / "Cache"}
    assert list((profile / "Cache").iterdir())


@pytest.mark.parametrize(
    "component", ["drive_c", "drive_c/users", "drive_c/users/steamuser",
                  "drive_c/users/steamuser/AppData", "drive_c/users/steamuser/AppData/Local"]
)
def test_discovery_never_scans_through_a_linked_ancestor(tmp_path, monkeypatch, component):
    prefix = tmp_path / "prefix with spaces"
    _profile(prefix, "Local", "CEF", "User Data")
    ancestor = prefix / component
    outside = tmp_path / "outside"
    ancestor.rename(outside)
    ancestor.symlink_to(outside, target_is_directory=True)
    scanned = []
    real_scandir = os.scandir

    def spy(folder):
        scanned.append(Path(folder))
        return real_scandir(folder)

    monkeypatch.setattr(storagecard.os, "scandir", spy)
    assert storagecard._web_profiles(prefix) == ()
    assert not any(path == ancestor or ancestor in path.parents for path in scanned)


def test_directory_budget_also_bounds_child_enumeration(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    local = _appdata(prefix)
    local.mkdir(parents=True)
    for index in range(60):
        (local / f"d{index:03d}").mkdir()
    monkeypatch.setattr(storagecard, "WEB_PROFILE_DIRS", 20)
    real_scandir = os.scandir
    inspected = []

    class CountingScan:
        def __init__(self, folder):
            self.scan = real_scandir(folder)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scan.close()

        def __iter__(self):
            for entry in self.scan:
                inspected.append(entry.path)
                yield entry

    monkeypatch.setattr(storagecard.os, "scandir", CountingScan)
    assert storagecard._web_profiles(prefix) == ()
    assert len(inspected) <= 20


@pytest.mark.parametrize("inside_prefix", [False, True])
def test_clear_rechecks_a_service_worker_link_inserted_after_scan(tmp_path, inside_prefix):
    prefix = tmp_path / "prefix"
    profile = _nikke_profile(prefix)
    entry = _scan(_config(("g", _game(prefix)))).webcaches[0]
    worker = profile / "Service Worker"
    outside = (prefix if inside_prefix else tmp_path) / "outside"
    worker.rename(outside)
    worker.symlink_to(outside, target_is_directory=True)

    result = storagecard.clear_webcache(entry)

    assert set(result.kept) == {worker / "CacheStorage", worker / "ScriptCache"}
    assert (outside / "CacheStorage/f0").read_bytes() == b"x" * 5
    assert (outside / "ScriptCache/f0").read_bytes() == b"x" * 7
    assert not list((profile / "Cache").iterdir())
