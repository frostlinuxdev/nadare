# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE has no install folder of its own (maintainer, 2026-11-11).

Its launcher installs inside the prefix (``C:\\NIKKE``); an install folder outside the prefix was
ignored by the game but shown in Settings and opened read-write to the game by the Sandbox. The
maintainer set it to an unrelated source folder and the Sandbox would have let NIKKE write there.
"""

from __future__ import annotations

from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import LauncherBackend, NikkeBackend
from nadare.core.backends import get


def _nikke(tmp_path, **profile):
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", **profile}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def test_settings_offers_no_install_folder_for_nikke():
    assert "install_dir" not in {o.key for o in get("launcher-nikke").options}


def test_a_generic_launcher_keeps_its_folder():
    assert "install_dir" in {o.key for o in LauncherBackend.options}


def test_the_sandbox_never_opens_a_folder_outside_the_prefix(tmp_path):
    unrelated = tmp_path / "src" / "docs"
    unrelated.mkdir(parents=True)
    backend = _nikke(tmp_path, install_dir=str(unrelated))
    assert backend._sandbox_game_dir() is None


def test_the_sandbox_has_nothing_extra_to_open_by_default(tmp_path):
    assert _nikke(tmp_path)._sandbox_game_dir() is None
