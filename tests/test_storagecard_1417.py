# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1417 / #1617: what the Disk space card lists, sizes and clears.

Every category is measured in a throw-away HOME: installers in Nadare's cache, each
Wine game's prefix ``Temp``, old Steam ``<appid>.bak-pre-*`` prefix copies and the
shader caches #1364 owns.  Clearing one category leaves the others alone, a missing
folder is not listed, and a link is never followed out of the listed path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nadare.core import installercache, paths, shadercache, storagecard

DAY = 86400
WV2 = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
VC = "vc_redist.x64.exe"


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


def _temp(prefix: Path, user: str = "steamuser", size: int = 100) -> Path:
    folder = prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Temp"
    return _dir(folder, files=2, size=size)


def _backup(compatdata: Path, name: str, size: int = 50, age: float = DAY) -> Path:
    return _dir(compatdata / name, files=1, size=size)


def _config(*profiles: tuple[str, dict]) -> dict:
    return {name: profile for name, profile in profiles}


def test_scan_sizes_every_category_in_a_fake_home(tmp_path: Path) -> None:
    _file(paths.cache_dir() / "czn" / WV2, size=215)
    _file(paths.cache_dir() / "wine" / VC, size=25)

    prefix = tmp_path / "prefix"
    _temp(prefix, "steamuser", 7)
    _temp(prefix, "nobody", 3)

    steamapps = tmp_path / "steamapps"
    compatdata = steamapps / "compatdata"
    _backup(compatdata, "5019180.bak-pre-webview2", 40, age=2 * DAY)
    _backup(compatdata, "5019180.bak-pre-webview2-2", 60, age=DAY)

    mesa = _dir(shadercache.mesa_dir(), files=1, size=11)
    vkd3d = _dir(shadercache.vkd3d_dir("czn"), files=1, size=5)

    report = storagecard.scan(
        _config(
            (
                "czn",
                {
                    "backend": "stove-czn",
                    "title": "CZN",
                    "prefix": str(prefix),
                },
            )
        ),
        steamapps=[steamapps],
        running=lambda _prefix: False,
    )

    assert report.installers.size == 215 + 25
    assert sorted(p.name for p in report.installers.files) == sorted([WV2, VC])
    assert len(report.temps) == 1
    assert report.temps[0].profile == "czn"
    assert report.temps[0].title == "CZN"
    assert report.temps[0].size == 2 * 7 + 2 * 3
    assert len(report.temps[0].folders) == 2
    assert report.temps[0].running is False
    assert [b.path.name for b in report.backups] == [
        "5019180.bak-pre-webview2",
        "5019180.bak-pre-webview2-2",
    ]
    assert [b.newest for b in report.backups] == [False, True]
    assert sum(b.size for b in report.backups) == 40 + 60
    layers = {row.label: row for row in report.shaders}
    assert layers["Mesa"].size == 11
    assert layers["VKD3D · CZN"].size == 5
    assert layers["Mesa"].folders == (mesa,)
    assert layers["VKD3D · CZN"].folders == (vkd3d,)


def test_missing_folders_are_not_listed(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    report = storagecard.scan(
        _config(("g", {"backend": "wine", "prefix": str(prefix)})),
        steamapps=[tmp_path / "no-steamapps"],
        running=lambda _prefix: False,
    )
    assert report.installers.size == 0 and report.installers.files == ()
    assert report.temps == ()
    assert report.backups == ()
    assert report.shaders == ()


def test_backups_are_grouped_and_the_newest_is_marked_per_appid(tmp_path: Path) -> None:
    compatdata = tmp_path / "steamapps" / "compatdata"
    _backup(compatdata, "5019180.bak-pre-webview2", 10, age=30 * DAY)
    _backup(compatdata, "5019180.bak-pre-webview2-2", 10, age=20 * DAY)
    _backup(compatdata, "3308670.bak-pre-vcredist", 10, age=2 * DAY)
    _backup(compatdata, "3308670.bak-pre-vcredist-2", 10, age=DAY)
    _backup(compatdata, "999.bak-pre-webview2", 10, age=DAY)

    report = storagecard.scan({}, steamapps=[tmp_path / "steamapps"], running=lambda _p: False)

    by_app: dict[str, list[storagecard.Backup]] = {}
    for backup in report.backups:
        by_app.setdefault(backup.app_id, []).append(backup)
    assert sorted(by_app) == ["3308670", "5019180", "999"]
    assert [b.newest for b in by_app["5019180"]] == [False, True]
    assert [b.newest for b in by_app["3308670"]] == [False, True]
    assert [b.newest for b in by_app["999"]] == [True]


def test_a_dangling_or_foreign_compatdata_entry_is_not_a_backup(tmp_path: Path) -> None:
    compatdata = tmp_path / "steamapps" / "compatdata"
    _dir(compatdata / "5019180", files=1)
    _file(compatdata / "5019180.bak-pre-webview2", size=10)  # a file, not a folder
    (compatdata / "5019180.bak-pre-webview2-2").symlink_to(tmp_path, target_is_directory=True)

    report = storagecard.scan({}, steamapps=[tmp_path / "steamapps"], running=lambda _p: False)
    assert report.backups == ()


def test_clear_installers_deletes_only_installers(tmp_path: Path) -> None:
    installer = _file(paths.cache_dir() / "czn" / WV2, size=30)
    prefix = tmp_path / "prefix"
    temp = _temp(prefix, size=5)
    compatdata = tmp_path / "steamapps" / "compatdata"
    backup = _backup(compatdata, "5019180.bak-pre-webview2", 50)

    report = storagecard.scan(
        _config(("g", {"backend": "wine", "prefix": str(prefix)})),
        steamapps=[tmp_path / "steamapps"],
        running=lambda _p: False,
    )
    result = storagecard.clear_installers(report.installers.files)

    assert result.freed == 30
    assert not installer.exists()
    assert temp.exists()
    assert backup.exists()


def test_clear_temp_deletes_only_that_games_temp(tmp_path: Path) -> None:
    prefix_a = tmp_path / "a"
    prefix_b = tmp_path / "b"
    temp_a = _temp(prefix_a, size=4)
    temp_b = _temp(prefix_b, size=6)
    report = storagecard.scan(
        _config(
            ("a", {"backend": "wine", "prefix": str(prefix_a)}),
            ("b", {"backend": "wine", "prefix": str(prefix_b)}),
        ),
        steamapps=[],
        running=lambda _p: False,
    )
    result = storagecard.clear_temp(report.temps[0])

    assert result.freed == 2 * 4
    # Wine does not recreate %TEMP% on every start: the folder stays, emptied.
    assert temp_a.is_dir()
    assert list(temp_a.iterdir()) == []
    assert temp_b.exists()
    assert list(temp_b.iterdir())


def test_clear_temp_refuses_while_the_game_is_running(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    temp = _temp(prefix)
    report = storagecard.scan(
        _config(("g", {"backend": "wine", "prefix": str(prefix)})),
        steamapps=[],
        running=lambda _p: True,
    )
    assert report.temps[0].running is True
    result = storagecard.clear_temp(report.temps[0])
    assert result.freed == 0
    assert result.kept == [temp]
    assert "running" in result.reason
    assert temp.exists()


def test_clear_backup_deletes_only_that_backup(tmp_path: Path) -> None:
    compatdata = tmp_path / "steamapps" / "compatdata"
    old = _backup(compatdata, "5019180.bak-pre-webview2", 10, age=2 * DAY)
    new = _backup(compatdata, "5019180.bak-pre-webview2-2", 20, age=DAY)
    other = _backup(compatdata, "3308670.bak-pre-webview2", 30)
    report = storagecard.scan({}, steamapps=[tmp_path / "steamapps"], running=lambda _p: False)
    result = storagecard.clear_backup(next(b for b in report.backups if b.path == old))

    assert result.freed == 10
    assert not old.exists()
    assert new.exists() and other.exists()


def test_clear_shader_deletes_only_that_cache(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    dxvk = _dir(
        prefix / "drive_c/users/steamuser/AppData/Local/dxvk", files=1, size=7
    )
    mesa = _dir(shadercache.mesa_dir(), files=1, size=11)
    vkd3d = _dir(shadercache.vkd3d_dir("g"), files=1, size=5)
    report = storagecard.scan(
        _config(("g", {"backend": "wine", "prefix": str(prefix), "title": "Game"})),
        steamapps=[],
        running=lambda _p: False,
    )
    vkd3d_row = next(row for row in report.shaders if row.label.startswith("VKD3D"))
    result = storagecard.clear_shader(vkd3d_row)

    assert result.freed == 5
    assert not vkd3d.exists()
    assert mesa.exists() and dxvk.exists()


@pytest.mark.parametrize("category", ["installer", "temp", "backup", "shader"])
def test_a_link_out_of_a_listed_path_is_refused_and_its_target_survives(
    tmp_path: Path, category: str
) -> None:
    outside = _dir(tmp_path / "outside", files=1, size=99)
    if category == "installer":
        _file(paths.cache_dir() / "czn" / WV2, size=10)
        link = paths.cache_dir() / "czn" / "linked.exe"
        link.symlink_to(outside / "f0")
        result = storagecard.clear_installers([link])
        assert result.kept == [link]
    elif category == "temp":
        prefix = tmp_path / "prefix"
        (prefix / "drive_c/users/steamuser/AppData/Local").mkdir(parents=True)
        link = prefix / "drive_c/users/steamuser/AppData/Local/Temp"
        link.symlink_to(outside, target_is_directory=True)
        entry = storagecard.PrefixTemp(
            profile="g", title="Game", prefix=prefix, folders=(link,), size=99, running=False
        )
        result = storagecard.clear_temp(entry)
        assert result.kept == [link]
    elif category == "backup":
        compatdata = tmp_path / "steamapps" / "compatdata"
        compatdata.mkdir(parents=True)
        link = compatdata / "5019180.bak-pre-webview2"
        link.symlink_to(outside, target_is_directory=True)
        entry = storagecard.Backup(
            app_id="5019180", path=link, compatdata=compatdata, size=99, newest=False
        )
        result = storagecard.clear_backup(entry)
        assert result.kept == [link]
    else:
        link = tmp_path / "shader-link"
        link.symlink_to(outside, target_is_directory=True)
        entry = storagecard.ShaderCache(
            key="mesa", label="Mesa", profile=None, folders=(link,), size=99, running=False
        )
        result = storagecard.clear_shader(entry)
        assert result.kept == [link]
    assert result.removed == []
    assert link.is_symlink()
    assert (outside / "f0").read_bytes() == b"x" * 99


def test_scan_never_follows_a_linked_category_folder(tmp_path: Path) -> None:
    outside = _dir(tmp_path / "outside", files=1, size=99)
    prefix = tmp_path / "prefix"
    (prefix / "drive_c/users/steamuser/AppData/Local").mkdir(parents=True)
    (prefix / "drive_c/users/steamuser/AppData/Local/Temp").symlink_to(
        outside, target_is_directory=True
    )
    report = storagecard.scan(
        _config(("g", {"backend": "wine", "prefix": str(prefix)})),
        steamapps=[],
        running=lambda _p: False,
    )
    assert report.temps == ()


def test_installercache_lists_the_known_installer_folders_only() -> None:
    wv2 = _file(paths.cache_dir() / "czn" / WV2, size=30)
    vc = _file(paths.cache_dir() / "wine" / VC, size=20)
    _file(paths.cache_dir() / "czn" / "notes.json", size=5)
    _file(paths.cache_dir() / "umu" / VC, size=7)  # a folder the table does not own
    found = installercache.installers()
    assert sorted(path for path, _size in found) == sorted([wv2, vc])
    assert sorted(size for _path, size in found) == [20, 30]


def test_clear_temp_refuses_a_folder_from_another_games_prefix(tmp_path: Path) -> None:
    prefix_a = tmp_path / "a"
    temp_b = _temp(tmp_path / "b", size=6)
    _temp(prefix_a, size=4)
    entry = storagecard.PrefixTemp(
        profile="a", title="A", prefix=prefix_a, folders=(temp_b,), size=6, running=False
    )

    result = storagecard.clear_temp(entry)

    assert result.removed == []
    assert list(temp_b.iterdir()), "another game's Temp was emptied"


@pytest.mark.parametrize("category", ["installer", "temp", "backup", "shader"])
def test_review_refuses_a_replaced_category_root(tmp_path, monkeypatch, category):
    """A listed parent replaced by a link must not redirect Clear to user data."""
    prefix = tmp_path / "prefix with spaces"
    temp = _temp(prefix, size=7)
    installer = _file(paths.cache_dir() / "czn" / WV2, size=11)
    steamapps = tmp_path / "steamapps"
    backup = _backup(steamapps / "compatdata", "123.bak-pre-fix", 13)
    _dir(shadercache.vkd3d_dir("g"), size=17)
    monkeypatch.setattr(storagecard, "is_running", lambda _p: False)
    report = storagecard.scan(
        {"g": {"backend": "wine", "prefix": str(prefix)}}, steamapps=[steamapps]
    )
    if category == "installer":
        root, target = paths.cache_dir(), installer

        def clear():
            return storagecard.clear_installers(report.installers.files)

    elif category == "temp":
        root, target = prefix, temp / "f0"

        def clear():
            return storagecard.clear_temp(report.temps[0])

    elif category == "backup":
        root, target = steamapps / "compatdata", backup / "f0"

        def clear():
            return storagecard.clear_backup(report.backups[0])

    else:
        root, target = prefix, temp / "f0"
        dxvk = _dir(prefix / "drive_c/users/steamuser/AppData/Local/dxvk", size=19)
        report = storagecard.scan(
            {"g": {"backend": "wine", "prefix": str(prefix)}}, steamapps=[]
        )
        target = dxvk / "f0"
        entry = next(row for row in report.shaders if row.key == "dxvk:g")

        def clear():
            return storagecard.clear_shader(entry)

    outside = root.with_name(root.name + "-outside")
    relative = target.relative_to(root)
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    result = clear()
    assert not result.removed
    assert (outside / relative).is_file(), "Clear followed the replaced parent"


def test_review_scan_skips_linked_intermediate_temp_and_shader_dirs(tmp_path):
    prefix = tmp_path / "prefix"
    local = prefix / "drive_c/users/steamuser/AppData/Local"
    _temp(prefix)
    _dir(local / "dxvk", size=5)
    outside = tmp_path / "outside"
    local.rename(outside)
    local.symlink_to(outside, target_is_directory=True)
    shaders = shadercache.vkd3d_dir("g").parent
    _dir(shaders / "vkd3d", size=9)
    shader_outside = tmp_path / "outside-shaders"
    shaders.rename(shader_outside)
    shaders.symlink_to(shader_outside, target_is_directory=True)
    report = storagecard.scan(
        {"g": {"prefix": str(prefix)}}, steamapps=[], running=lambda _p: False
    )
    assert report.temps == ()
    assert report.shaders == ()


def test_review_vkd3d_rechecks_running_before_clear(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    cache = _dir(shadercache.vkd3d_dir("g"), size=17)
    report = storagecard.scan(
        {"g": {"prefix": str(prefix)}}, steamapps=[], running=lambda _p: False
    )
    monkeypatch.setattr(storagecard, "is_running", lambda p: p == prefix)
    result = storagecard.clear_shader(report.shaders[0])
    assert result.removed == []
    assert result.kept == [cache]
    assert (cache / "f0").exists()


def test_review_newest_backup_uses_copy_time_not_source_mtime(tmp_path):
    """copytree preserves prefix mtime, which need not advance between fixes."""
    compatdata = tmp_path / "steamapps/compatdata"
    old = _backup(compatdata, "123.bak-pre-webview2", 10)
    os.utime(old, (time.time() + DAY, time.time() + DAY))
    time.sleep(0.01)  # ensure distinct filesystem change times
    new = _backup(compatdata, "123.bak-pre-vcredist", 20)
    os.utime(new, (1, 1))
    report = storagecard.scan({}, steamapps=[compatdata.parent])
    assert next(b for b in report.backups if b.path == new).newest
    assert not next(b for b in report.backups if b.path == old).newest


def test_review_shared_mesa_rechecks_all_prefixes(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    mesa = _dir(shadercache.mesa_dir(), size=7)
    report = storagecard.scan(
        {"g": {"prefix": str(prefix)}}, steamapps=[], running=lambda _p: False
    )
    monkeypatch.setattr(storagecard, "is_running", lambda p: p == prefix)
    result = storagecard.clear_shader(report.shaders[0])
    assert result.removed == []
    assert result.kept == [mesa]
    assert (mesa / "f0").is_file()


def test_review_shader_scan_keeps_unknown_and_empty_caches(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _dir(shadercache.mesa_dir(), size=5)
    shadercache.vkd3d_dir("g").mkdir(parents=True)
    first = prefix / "drive_c/users/first/AppData/Local/dxvk"
    second = prefix / "drive_c/users/second/AppData/Local/dxvk"
    _dir(first, size=11)
    _dir(second, size=13)
    original = shadercache.cache_size
    monkeypatch.setattr(
        shadercache,
        "cache_size",
        lambda p, **kwargs: (
            None if p in (shadercache.mesa_dir(), second) else original(p, **kwargs)
        ),
    )
    report = storagecard.scan(
        {"g": {"prefix": str(prefix)}}, steamapps=[], running=lambda _p: False
    )
    layers = {row.key: row for row in report.shaders}
    assert layers["mesa"].size is None
    assert layers["vkd3d:g"].size == 0
    assert layers["dxvk:g"].size is None, "a partial sum must not be shown as the total"
