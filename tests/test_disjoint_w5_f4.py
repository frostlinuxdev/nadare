# SPDX-License-Identifier: GPL-3.0-or-later
"""W5-F4: the two "Steam is running" detectors disagree for a Flatpak Steam client."""

from __future__ import annotations

from pathlib import Path

from nadare.core import shortcuts, steamtools


def _fake_proc(root: Path, pid: int, argv: list[str], comm: str) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")


def test_a_flatpak_steam_client_counts_as_running_for_shortcuts_too(tmp_path):
    proc = tmp_path / "proc"
    # The visible Flatpak wrapper: no process is named "steam"; the argv names
    # the Steam Flatpak id, which is exactly what steamtools recognises.
    _fake_proc(proc, 54321, ["flatpak", "run", "com.valvesoftware.Steam"], "bwrap")

    detected_by_steamtools = steamtools.steam_pids(proc_root=proc)
    detected_by_shortcuts = shortcuts.steam_running(proc_root=proc)

    assert detected_by_steamtools == [54321], "fixture: steamtools must see the Flatpak client"
    assert detected_by_shortcuts is True, (
        "shortcuts.steam_running says Steam is closed (comm is 'bwrap', not 'steam'), so "
        "Nadare would rewrite shortcuts.vdf while Flatpak Steam runs and Steam overwrites it "
        f"on exit; steamtools.steam_pids={detected_by_steamtools}"
    )
