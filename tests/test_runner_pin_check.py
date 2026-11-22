# SPDX-License-Identifier: GPL-3.0-or-later
"""The pinned runner is only a folder that really is that release.

A b24 player's every game crash-looped inside Wine's kernelbase until they picked DW-Proton by
hand; the crash address fits none of the pinned builds. ``installed_pinned`` took any folder whose
name merely contained the pinned tag (``…-11.0-17-x86_64_v3``, ``…-11.0-17-custom``), and any
copy of that name under Steam's tool folders, without looking inside. Nadare's own downloads are
sha256-checked at install; a copy from elsewhere has to show it is the release.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import runners

DLLS = ("ntdll.dll", "kernel32.dll", "kernelbase.dll")


@pytest.fixture
def own(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    return folder


@pytest.fixture
def steam(tmp_path, monkeypatch):
    folder = tmp_path / "compatibilitytools.d"
    folder.mkdir()
    monkeypatch.setattr(
        runners, "steam_runners", lambda: sorted(p for p in folder.iterdir() if p.is_dir())
    )
    return folder


def _make(
    root: Path,
    name: str,
    *,
    version: str | None = None,
    dlls: tuple[str, ...] = DLLS,
    lib: str = "lib",
    wine: bool = True,
) -> Path:
    d = root / name
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    if wine:
        (d / "files" / "bin").mkdir(parents=True)
        (d / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    win = d / "files" / lib / "wine" / "x86_64-windows"
    win.mkdir(parents=True)
    for dll in dlls:
        (win / dll).write_bytes(b"MZ")
    if version is not None:
        (d / "version").write_text(version + "\n")
    return d


# --- the folder name --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "dwproton-11.0-17-x86_64_v3",
        "dwproton-11.0-17-custom",
        "dwproton-11.0-17-x86_64-old",
        "dwproton-11.0-17_backup",
    ],
)
def test_a_name_with_more_after_the_tag_is_not_the_pin(own, steam, name):
    _make(own, name)
    assert runners.installed_pinned("dwproton") is None


@pytest.mark.parametrize(
    ("family", "name"),
    [
        ("dwproton", "dwproton-11.0-17-x86_64"),
        ("dwproton", "dwproton-11.0-17"),
        ("dwproton", "DWProton-11.0-17-x86_64"),
        ("ge", "GE-Proton11-7"),
        ("ge", "GE-Proton11-7-x86_64"),
        ("cachyos", "proton-cachyos-11.0-20260703-slr-x86_64"),
    ],
)
def test_the_names_the_release_archives_unpack_to_are_the_pin(own, steam, family, name):
    pinned = _make(own, name)
    assert runners.installed_pinned(family) == pinned


def test_a_bad_name_does_not_hide_a_good_one(own, steam):
    _make(own, "dwproton-11.0-17-x86_64_v3")
    good = _make(own, "dwproton-11.0-17-x86_64")
    assert runners.installed_pinned("dwproton") == good


# --- a copy Nadare did not download ---------------------------------------------------------------
def test_a_steam_copy_that_is_the_release_is_reused(own, steam):
    copy = _make(steam, "dwproton-11.0-17-x86_64", version="1794581887 dwproton-11.0-17")
    assert runners.installed_pinned("dwproton") == copy


def test_a_steam_copy_with_lib64_is_reused(own, steam):
    copy = _make(steam, "GE-Proton11-7", version="1789520217 GE-Proton11-7", lib="lib64")
    assert runners.installed_pinned("ge") == copy


def test_a_steam_copy_without_a_version_file_is_not_the_pin(own, steam):
    _make(steam, "dwproton-11.0-17-x86_64")
    assert runners.installed_pinned("dwproton") is None


def test_a_steam_copy_whose_version_names_another_build_is_not_the_pin(own, steam):
    # A renamed folder: the name says 11.0-17, the build inside is something else.
    _make(steam, "dwproton-11.0-17-x86_64", version="1789000000 dwproton-11.0-13")
    assert runners.installed_pinned("dwproton") is None


@pytest.mark.parametrize("missing", DLLS)
def test_a_steam_copy_missing_a_core_dll_is_not_the_pin(own, steam, missing):
    _make(
        steam,
        "dwproton-11.0-17-x86_64",
        version="1794581887 dwproton-11.0-17",
        dlls=tuple(d for d in DLLS if d != missing),
    )
    assert runners.installed_pinned("dwproton") is None


def test_a_steam_copy_without_wine_is_not_the_pin(own, steam):
    _make(steam, "dwproton-11.0-17-x86_64", version="1794581887 dwproton-11.0-17", wine=False)
    assert runners.installed_pinned("dwproton") is None


def test_nadares_own_copy_wins_over_a_steam_copy(own, steam):
    _make(steam, "dwproton-11.0-17-x86_64", version="1794581887 dwproton-11.0-17")
    mine = _make(own, "dwproton-11.0-17-x86_64", version="1794581887 dwproton-11.0-17")
    assert runners.installed_pinned("dwproton") == mine


def test_a_bad_steam_copy_means_the_pin_is_downloaded(own, steam, monkeypatch):
    _make(steam, "dwproton-11.0-17-x86_64")  # no version file
    calls: list[str] = []

    def fake_install(family, *, latest=False, emit=None, cancel=None):
        calls.append(family)
        return _make(own, "dwproton-11.0-17-x86_64")

    monkeypatch.setattr(runners, "install", fake_install)
    got = runners.ensure_pinned("dwproton")
    assert calls == ["dwproton"]
    assert got.parent == own
