# SPDX-License-Identifier: GPL-3.0-or-later
"""The move to the Flatpak sees a game running outside Nadare, as Remove does (C3, 2026-11-21).

The move renames Nadare's data folder, prefixes included. It asked only Nadare's run locks, and
only for profiles that name their prefix, so a game started from its own desktop entry, through
Steam or a leftover wineserver passed the check and had its prefix renamed under it. Remove asks
for Wine processes in the prefix too and refuses when it cannot tell; the move now does the same.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_1353_move_to_flatpak import FakeFlatpak, _appimage_world, home  # noqa: F401

from nadare.core import flatpakmove, paths


def _fake_proc(root: Path, *envs: dict[str, str]) -> Path:
    """A /proc with one process per environment (owned by this user, so the scan keeps it)."""
    root.mkdir(parents=True, exist_ok=True)
    for pid, env in enumerate(envs, start=4000):
        entry = root / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"wineserver\0")
        (entry / "environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))
    return root


def _refusal(cfg, home, proc: Path) -> str | None:  # noqa: F811
    return flatpakmove.refusal(cfg, home=home, run=FakeFlatpak(), proc_root=proc)


def _with_default_prefix_game(home):  # noqa: F811
    """WuWa/CZN as in the move tests, plus NTE that sets no prefix (profiles/nte/prefix)."""
    cfg = _appimage_world(home)
    default = paths.profiles_dir() / "nte" / "prefix"
    (default / "drive_c").mkdir(parents=True)
    cfg.add_profile("nte", backend="wine", title="NTE")
    cfg.save()
    return cfg, default


def test_wine_in_a_game_prefix_refuses(home, tmp_path):  # noqa: F811
    cfg = _appimage_world(home)
    proc = _fake_proc(tmp_path / "proc", {"WINEPREFIX": cfg.profiles["wuwa"]["prefix"]})
    reason = _refusal(cfg, home, proc)
    assert reason and "WuWa" in reason


def test_wine_in_a_default_prefix_refuses(home, tmp_path):  # noqa: F811
    cfg, default = _with_default_prefix_game(home)
    proc = _fake_proc(tmp_path / "proc", {"WINEPREFIX": str(default)})
    reason = _refusal(cfg, home, proc)
    assert reason and "NTE" in reason


def test_a_proton_compat_path_counts(home, tmp_path):  # noqa: F811
    cfg = _appimage_world(home)
    proc = _fake_proc(tmp_path / "proc", {"STEAM_COMPAT_DATA_PATH": cfg.profiles["czn"]["prefix"]})
    reason = _refusal(cfg, home, proc)
    assert reason and "CZN" in reason


def test_wine_in_a_prefix_no_game_names_inside_the_data_folder_refuses(home, tmp_path):  # noqa: F811
    """A leftover wineserver of a removed game: its prefix still moves with the data folder."""
    cfg = _appimage_world(home)
    stray = paths.data_dir() / "profiles" / "gone" / "prefix"
    stray.mkdir(parents=True)
    proc = _fake_proc(tmp_path / "proc", {"WINEPREFIX": str(stray)})
    reason = _refusal(cfg, home, proc)
    assert reason and "running" in reason.lower()


def test_unrelated_wine_does_not_refuse(home, tmp_path):  # noqa: F811
    cfg = _appimage_world(home)
    other = home / ".wine"
    other.mkdir()
    proc = _fake_proc(
        tmp_path / "proc", {"WINEPREFIX": str(other)}, {"PATH": os.environ.get("PATH", "")}
    )
    assert _refusal(cfg, home, proc) is None


def test_a_scan_that_cannot_answer_refuses(home, tmp_path, monkeypatch):  # noqa: F811
    cfg = _appimage_world(home)

    def broken(*_a, **_k):
        raise PermissionError("no /proc")

    monkeypatch.setattr(flatpakmove.process, "ProcessScanRound", broken)
    reason = _refusal(cfg, home, tmp_path / "proc")
    assert reason and "could not check" in reason
    assert "Nothing was changed" in reason


def test_the_real_proc_is_the_default(home):  # noqa: F811
    """No proc_root: the real /proc, where nothing runs in these temp prefixes."""
    cfg = _appimage_world(home)
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak()) is None


@pytest.mark.parametrize("suffix", ["", "/pfx"])
def test_the_pfx_folder_counts_as_the_prefix(home, tmp_path, suffix):  # noqa: F811
    cfg = _appimage_world(home)
    proc = _fake_proc(tmp_path / "proc", {"WINEPREFIX": cfg.profiles["wuwa"]["prefix"] + suffix})
    assert _refusal(cfg, home, proc)
