# SPDX-License-Identifier: GPL-3.0-or-later
"""umu's own download leftovers (#1417): ``~/.local/share/umu/.<8 chars>/``.

umu downloads and unpacks the Steam Runtime in ``TemporaryDirectory(dir=<umu folder>,
prefix=".")`` while it holds ``umu.lock``. A Stop, a closed Nadare or a power cut kills it
there, and nothing ever removes that folder (kill test 2: 210 MB; flxpc: 985 MB). The
runtimes themselves are shared with other umu apps and are never touched.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from nadare.core import leftovers

DAY = 86400


def _old(path: Path, age: float = 2 * DAY) -> Path:
    t = time.time() - age
    os.utime(path, (t, t), follow_symlinks=False)
    return path


def _dir(path: Path, age: float = 2 * DAY, content: str = "SteamLinuxRuntime_4.tar.xz") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if content:
        (path / content).write_bytes(b"x" * 1000)
    return _old(path, age)


@pytest.fixture
def umu_home() -> Path:
    root = Path.home() / ".local" / "share" / "umu"
    root.mkdir(parents=True)
    return root


def test_a_killed_runtime_download_is_removed(umu_home: Path) -> None:
    temp = _dir(umu_home / ".3w3yy4xh")
    result = leftovers.sweep()
    assert not temp.exists()
    assert temp in result.removed and result.freed >= 1000


def test_the_runtimes_and_umus_own_files_are_kept(umu_home: Path) -> None:
    kept = [
        _dir(umu_home / "steamrt4"),
        _dir(umu_home / "steamrt3"),
        _dir(umu_home / "compatibilitytools"),
        # hidden folders of something else, even with an 8-character name
        _dir(umu_home / ".steamrt4", content="VERSIONS.txt"),
        _dir(umu_home / ".a1b2c3d4", content="save.dat"),
        _dir(umu_home / ".abcdefghi"),  # 9 characters
        _dir(umu_home / ".abc-efgh"),  # not a tempfile character
    ]
    lock = umu_home / "umu.lock"
    lock.touch()
    shim = umu_home / "umu-shim"
    shim.write_text("#!/bin/sh\n")
    leftovers.sweep()
    for path in kept:
        assert path.exists(), path
    assert lock.exists() and shim.exists()


def test_umus_download_shapes_are_all_removed(umu_home: Path) -> None:
    """Killed before the move (empty), after it (archive), while unpacking or after the swap."""
    empty = _dir(umu_home / ".aaaa1111", content="")
    archive = _dir(umu_home / ".bbbb2222", content="SteamLinuxRuntime_sniper.tar.xz")
    tree = _dir(umu_home / ".cccc3333", content="SteamLinuxRuntime_4")
    leftovers.sweep()
    assert not empty.exists() and not archive.exists() and not tree.exists()


def test_a_temp_file_with_the_name_is_not_a_folder_and_is_kept(umu_home: Path) -> None:
    item = umu_home / ".a1b2c3d4"
    item.write_text("not ours")
    _old(item)
    leftovers.sweep()
    assert item.exists()


def test_a_young_temp_folder_is_kept(umu_home: Path) -> None:
    temp = _dir(umu_home / ".91wbo6nw", age=3600)
    leftovers.sweep()
    assert temp.exists()


def test_nothing_is_removed_while_umu_holds_its_lock(umu_home: Path, tmp_path: Path) -> None:
    temp = _dir(umu_home / ".91wbo6nw")
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            "import fcntl,sys,time,pathlib;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);"
            "pathlib.Path(sys.argv[2]).touch();time.sleep(30)",
            str(umu_home / "umu.lock"),
            str(ready),
        ]
    )
    try:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        assert leftovers.sweep().removed == []
        assert temp.exists()
    finally:
        holder.kill()
        holder.wait()
    leftovers.sweep()
    assert not temp.exists()


def test_the_sweep_holds_umus_lock_while_it_removes(umu_home: Path, monkeypatch) -> None:
    """A umu that starts meanwhile waits for the lock instead of racing the removal."""
    temp = _dir(umu_home / ".91wbo6nw")
    seen: list[bool] = []
    real_remove = leftovers._remove

    def remove(path, st, result):
        fd = os.open(umu_home / "umu.lock", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            seen.append(True)
        else:
            seen.append(False)
        finally:
            os.close(fd)
        real_remove(path, st, result)

    monkeypatch.setattr(leftovers, "_remove", remove)
    leftovers.sweep()
    assert seen == [True] and not temp.exists()


def test_a_temp_folder_that_is_a_link_is_left_alone(umu_home, tmp_path) -> None:
    outside = _dir(tmp_path / "precious")
    link = umu_home / ".a1b2c3d4"
    link.symlink_to(outside, target_is_directory=True)
    _old(link)
    leftovers.sweep()
    assert outside.exists() and (outside / "SteamLinuxRuntime_4.tar.xz").exists()
    assert link.is_symlink()  # umu never makes links there: not its leftover


def test_an_umu_folder_that_is_a_link_is_not_swept(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    temp = _dir(real / ".a1b2c3d4")
    share = Path.home() / ".local" / "share"
    share.mkdir(parents=True)
    (share / "umu").symlink_to(real, target_is_directory=True)
    leftovers.sweep()
    assert temp.exists()


def test_umus_xdg_data_home_folder_is_swept_too(monkeypatch, tmp_path: Path) -> None:
    """Outside the Flatpak umu follows XDG_DATA_HOME."""
    xdg = tmp_path / "xdgdata"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setattr(leftovers.sandbox, "in_flatpak", lambda: False)
    temp = _dir(xdg / "umu" / ".zz11yy22")
    leftovers.sweep()
    assert not temp.exists()


def test_inside_the_flatpak_xdg_data_home_is_not_umus(monkeypatch, tmp_path: Path) -> None:
    """In the Flatpak umu uses ~/.local/share/umu; XDG_DATA_HOME is the app's own folder."""
    xdg = tmp_path / "appdata"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setattr(leftovers.sandbox, "in_flatpak", lambda: True)
    temp = _dir(xdg / "umu" / ".zz11yy22")
    leftovers.sweep()
    assert temp.exists()


def test_a_missing_umu_folder_is_fine() -> None:
    assert leftovers.sweep().removed == []
