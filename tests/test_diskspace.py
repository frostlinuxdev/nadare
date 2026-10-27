# SPDX-License-Identifier: GPL-3.0-or-later
"""Disk-space helpers: statvfs lookup, human sizes and the space guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nadare.core import diskspace
from nadare.core.backends import BackendError


def _statvfs(free_blocks: int, frsize: int = 4096) -> SimpleNamespace:
    return SimpleNamespace(f_bavail=free_blocks, f_frsize=frsize)


def test_free_bytes_uses_nearest_existing_ancestor(tmp_path, monkeypatch):
    seen: list[object] = []

    def fake_statvfs(path):
        seen.append(path)
        return _statvfs(100, 1024)

    monkeypatch.setattr(diskspace.os, "statvfs", fake_statvfs)
    missing = tmp_path / "does" / "not" / "exist"
    assert diskspace.free_bytes(missing) == 100 * 1024
    assert seen == [tmp_path]


def test_human_size_formats_ranges():
    assert diskspace.human_size(0) == "0 B"
    assert diskspace.human_size(512) == "512 B"
    assert diskspace.human_size(1536) == "1.5 KiB"
    assert diskspace.human_size(5 * (1 << 20)) == "5 MiB"
    assert diskspace.human_size(319 * (1 << 30)) == "319 GiB"


def test_check_space_raises_with_mount_and_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(diskspace.os, "statvfs", lambda _p: _statvfs(0, 1))
    monkeypatch.setattr(diskspace, "mount_point", lambda _p: "/mnt/games")

    with pytest.raises(BackendError) as excinfo:
        diskspace.check_space(tmp_path, 1000, margin=0.05)

    assert excinfo.value.code == 4
    assert excinfo.value.message == ("Not enough disk space on /mnt/games: need 1 KiB, 0 B free")


def test_check_space_accepts_when_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(diskspace.os, "statvfs", lambda _p: _statvfs(1000, 1))
    monkeypatch.setattr(diskspace, "mount_point", lambda _p: "/mnt/games")
    diskspace.check_space(tmp_path, 100, margin=0.05)  # 105 needed, 1000 free


def test_check_space_ignores_nonpositive_need(tmp_path, monkeypatch):
    monkeypatch.setattr(diskspace.os, "statvfs", lambda _p: _statvfs(0, 1))
    diskspace.check_space(tmp_path, 0)


def test_free_bytes_reports_backend_error_on_vanishing_path(tmp_path, monkeypatch):
    """L12: a path that vanishes before statvfs must not leak a raw OSError."""

    def boom(_path):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(diskspace.os, "statvfs", boom)
    with pytest.raises(BackendError):
        diskspace.free_bytes(tmp_path / "gone")
    with pytest.raises(BackendError):
        diskspace.check_space(tmp_path / "gone", 1000)


def test_disk_space_report_degrades_on_statvfs_failure(tmp_path, monkeypatch):
    """Regression (02-NV-1): the doctor disk report catches the BackendError
    that ``free_bytes`` now raises and degrades to an ``unknown`` line."""
    from nadare.app import _disk_space_report
    from nadare.core.config import Config

    def boom(_path):
        raise BackendError("cannot read free space")

    monkeypatch.setattr(diskspace, "free_bytes", boom)
    monkeypatch.setattr(diskspace, "mount_point", lambda _p: "/mnt/games")

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="wine", install_dir=str(tmp_path / "game"))

    # ``paths.data_dir()`` and the profile's install_dir share the fake mount;
    # the first candidate emits the unknown hint, the second is deduplicated.
    assert _disk_space_report(cfg) == ["  unknown free on /mnt/games"]


def test_human_size_uses_three_significant_digits_and_trims_a_zero_decimal():
    h = diskspace.human_size
    assert h(1 << 30) == "1 GiB"
    assert h(int(1.5 * (1 << 30))) == "1.5 GiB"  # was rounded to "2 GiB"
    assert h(int(1.04 * (1 << 30))) == "1 GiB"
    assert h(int(9.96 * (1 << 20))) == "10 MiB"  # never "10.0"
    assert h(int(12.6 * (1 << 20))) == "13 MiB"
    assert h(1023) == "1023 B"
    assert h(1024 * 1024 - 1) == "1 MiB"  # rounds up into the next unit, not "1024 KiB"
    assert h(1 << 40) == "1 TiB"
    assert h(-5) == "0 B"


def test_human_speed():
    assert diskspace.human_speed(0) == "-- MiB/s"
    assert diskspace.human_speed(52 * (1 << 20)) == "52 MiB/s"
    assert diskspace.human_speed(int(2.5 * (1 << 20))) == "2.5 MiB/s"
    assert diskspace.human_speed(300) == "300 B/s"


def test_every_ui_size_formatter_is_the_shared_one():
    from nadare.core import yostar
    from nadare.gui import logs_dialog, runners_dialog

    for size in (0, 700, 1536, 5 << 20, int(1.5 * (1 << 30)), 88 << 30):
        expected = diskspace.human_size(size)
        assert runners_dialog.human_size(size) == expected
        assert logs_dialog._human_size(size) == expected
        assert yostar._format_size(size) == expected
