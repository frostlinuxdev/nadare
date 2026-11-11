# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak shortcuts went to a private scratch folder (measured on the rig, 2026-11-11).

``_host_path`` turned every host path into ``/run/host/<path>``. Inside Nadare's Flatpak the home
folder is shared at its own path, and ``/run/host/home/<user>`` does not exist: "installed
/run/host/home/<user>/Desktop/nadare-bd2.desktop" landed in the sandbox's own scratch space and
never reached the desktop or the app menu (b22 Flatpak, with and without --filesystem=host). The
Steam scan looked there too and found no ``shortcuts.vdf``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import sandbox, shortcuts


@pytest.fixture
def flatpak(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    host_root = tmp_path / "run-host"
    host_root.mkdir()
    monkeypatch.setattr(shortcuts, "HOST_ROOT", host_root)
    return host_root


def test_a_host_folder_shared_at_its_own_path_is_used_as_is(flatpak, tmp_path):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    desktop = home / "Desktop"  # need not exist yet
    assert shortcuts._host_path(desktop) == desktop


ABSENT = Path("/nadare-test-absent-7f3c")  # a top-level folder no machine has


def test_only_a_folder_the_sandbox_cannot_see_goes_through_run_host(flatpak, tmp_path):
    hidden = ABSENT / "me" / "Desktop"
    mapped = flatpak / hidden.relative_to("/")
    mapped.parent.mkdir(parents=True)
    assert shortcuts._host_path(hidden) == mapped


def test_neither_visible_keeps_the_real_path(flatpak, tmp_path):
    hidden = ABSENT / "Desktop"
    assert shortcuts._host_path(hidden) == hidden


def test_outside_a_flatpak_nothing_is_mapped(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    assert shortcuts._host_path(tmp_path / "x") == tmp_path / "x"


def test_the_applications_folder_is_the_real_one(flatpak, tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    (home / ".local" / "share").mkdir(parents=True)
    monkeypatch.setattr(shortcuts, "_host_home", lambda: home)
    monkeypatch.delenv("HOST_XDG_DATA_HOME", raising=False)
    assert shortcuts._applications_dir(None) == home / ".local" / "share" / "applications"


def test_the_steam_scan_looks_in_the_real_home(flatpak, tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    vdf = home / ".steam" / "steam" / "userdata" / "123" / "config" / "shortcuts.vdf"
    vdf.parent.mkdir(parents=True)
    vdf.write_bytes(b"\x00shortcuts\x00\x08\x08")
    monkeypatch.setattr(shortcuts, "_host_home", lambda: home)
    found = shortcuts.find_steam_shortcut_files()
    assert [Path(p).resolve() for p in found] == [vdf.resolve()]


def test_when_both_exist_the_shared_path_wins(flatpak, tmp_path):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    (flatpak / home.relative_to("/")).mkdir(parents=True)  # a stale twin under /run/host
    assert shortcuts._host_path(home / "Desktop") == home / "Desktop"
