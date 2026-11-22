# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1364 phase 3: the Disk space card's shader cache warnings.

A game whose VKD3D and DXVK caches together pass 1 GiB gets a short note on its
rows.  All shader caches together warn above 8 GiB or above 5% of the free space
on Nadare's cache disk, whichever is smaller.  A size the bounded walk could not
measure (``None``) never warns and never crashes the scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import paths, shadercache, storagecard

GIB = 1024**3
GAME_TEXT = "Large: over 1 GB. Clearing is safe; the game may stutter while it rebuilds."


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dxvk(prefix: Path, user: str = "steamuser") -> Path:
    return _dir(prefix / "drive_c" / "users" / user / "AppData" / "Local" / "dxvk")


def _sizes(monkeypatch: pytest.MonkeyPatch, sizes: dict[Path, int | None]) -> None:
    monkeypatch.setattr(storagecard, "_cache_bytes", lambda folder: sizes.get(folder))


def _scan(monkeypatch, profiles, sizes, free=1 << 50):
    _sizes(monkeypatch, sizes)
    monkeypatch.setattr(storagecard.diskspace, "free_bytes", lambda _path: free)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    return storagecard.scan(profiles, steamapps=[], running=lambda _prefix: False)


def test_thresholds_are_module_constants() -> None:
    assert storagecard.GAME_WARN_BYTES == GIB
    assert storagecard.TOTAL_WARN_BYTES == 8 * GIB
    assert storagecard.TOTAL_WARN_SHARE == 0.05


def test_a_games_vkd3d_and_dxvk_together_over_one_gib_warn_on_its_rows(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix"
    _dir(prefix)
    vkd3d = _dir(shadercache.vkd3d_dir("g"))
    dxvk = _dxvk(prefix)
    report = _scan(
        monkeypatch,
        {"g": {"prefix": str(prefix), "title": "Game"}},
        {vkd3d: 600 * 1024**2, dxvk: 600 * 1024**2},
    )

    rows = [row for row in report.shaders if row.profile == "g"]
    assert {row.key for row in rows} == {"vkd3d:g", "dxvk:g"}
    assert [row.warning for row in rows] == [GAME_TEXT, ""]
    assert report.shader_warning == ""


def test_a_game_under_one_gib_has_no_warning(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _dir(prefix)
    vkd3d = _dir(shadercache.vkd3d_dir("g"))
    dxvk = _dxvk(prefix)
    report = _scan(
        monkeypatch,
        {"g": {"prefix": str(prefix)}},
        {vkd3d: 500 * 1024**2, dxvk: 500 * 1024**2},
    )

    assert all(not row.warning for row in report.shaders)


def test_unmeasured_sizes_never_warn_or_crash(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _dir(prefix)
    vkd3d = _dir(shadercache.vkd3d_dir("g"))
    dxvk = _dxvk(prefix)
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(
        monkeypatch,
        {"g": {"prefix": str(prefix)}},
        {vkd3d: None, dxvk: None, mesa: None},
    )

    assert len(report.shaders) == 3
    assert all(row.size is None for row in report.shaders)
    assert all(not row.warning for row in report.shaders)
    assert report.shader_warning == ""


def test_a_partly_measured_game_uses_only_the_known_bytes(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _dir(prefix)
    vkd3d = _dir(shadercache.vkd3d_dir("g"))
    dxvk = _dxvk(prefix)
    report = _scan(
        monkeypatch,
        {"g": {"prefix": str(prefix)}},
        {vkd3d: None, dxvk: 2 * GIB},
    )

    rows = {row.key: row for row in report.shaders}
    assert rows["vkd3d:g"].warning == ""
    assert rows["dxvk:g"].warning == GAME_TEXT


def test_all_caches_over_eight_gib_warn_once(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(monkeypatch, {}, {mesa: 9 * GIB}, free=1 << 50)

    assert report.shader_warning.startswith("Large:")
    assert "Clearing is safe" in report.shader_warning


def test_all_caches_under_eight_gib_do_not_warn(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(monkeypatch, {}, {mesa: 8 * GIB}, free=1 << 50)

    assert report.shader_warning == ""


def test_five_percent_of_free_space_is_used_when_it_is_the_smaller_limit(
    tmp_path, monkeypatch
):
    mesa = _dir(shadercache.mesa_dir())
    free = 20 * GIB  # 5% -> 1 GiB
    report = _scan(monkeypatch, {}, {mesa: 2 * GIB}, free=free)

    assert report.shader_warning.startswith("Large:")


def test_the_five_percent_limit_is_not_warned_at_exactly(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(monkeypatch, {}, {mesa: GIB}, free=20 * GIB)  # 5% = 1 GiB

    assert report.shader_warning == ""


def test_unreadable_free_space_falls_back_to_eight_gib(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())

    def boom(_path):
        raise OSError("no statvfs here")

    _sizes(monkeypatch, {mesa: 9 * GIB})
    monkeypatch.setattr(storagecard.diskspace, "free_bytes", boom)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    report = storagecard.scan({}, steamapps=[], running=lambda _prefix: False)

    assert report.shader_warning.startswith("Large:")


def test_a_full_disk_still_warns_about_a_measured_cache(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(monkeypatch, {}, {mesa: GIB}, free=0)

    assert report.shader_warning.startswith("Large:")


def test_a_full_disk_with_nothing_measured_stays_silent(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    report = _scan(monkeypatch, {}, {mesa: 0}, free=0)

    assert report.shader_warning == ""


def test_the_warning_never_counts_an_unknown_row_as_zero(tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir())
    other = _dir(paths.shader_cache_dir() / "games" / "other" / "vkd3d")
    report = _scan(
        monkeypatch,
        {"other": {"prefix": str(tmp_path / "missing")}},
        {mesa: 7 * GIB, other: None},
        free=1 << 50,
    )

    assert report.shader_warning == ""
