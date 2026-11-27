# SPDX-License-Identifier: GPL-3.0-or-later
"""A drive mounted noexec is refused before install and before Play (#1861, 2026-11-26).

A user's PGR stopped with only "exit code 5": the install drive was mounted ``noexec`` (an
fstab ``users`` option adds it silently), so Wine could not map the game's files.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import diskspace, execmount, flatpakgl, launchgate, nvidiarestart
from nadare.core.backends import BackendError


@pytest.fixture
def noexec(monkeypatch, tmp_path):
    """Make every path under ``tmp_path / "bad"`` look like a noexec filesystem."""
    bad = tmp_path / "bad"
    bad.mkdir()
    real = os.statvfs

    class Result:
        def __init__(self, result, flag):
            self._result, self.f_flag = result, flag

        def __getattr__(self, name):
            return getattr(self._result, name)

    def fake(path):
        result = real(path)
        if str(path).startswith(str(bad)):
            return Result(result, result.f_flag | os.ST_NOEXEC)
        return Result(result, result.f_flag & ~os.ST_NOEXEC)

    monkeypatch.setattr(execmount.os, "statvfs", fake)
    monkeypatch.setattr(
        execmount, "mount_point", lambda path: str(bad) if str(path).startswith(str(bad)) else "/"
    )
    return bad


class _Backend:
    kind = "wine-test"

    def __init__(self, install: Path, prefix: Path, log=None) -> None:
        self._install, self._prefix, self.log = install, prefix, log

    def install_dir(self) -> Path:
        return self._install

    def prefix(self) -> Path:
        return self._prefix

    def launch(self):
        if self.log is not None:
            self.log.append("launch")
        return 0


def test_a_noexec_folder_is_named(noexec):
    assert execmount.noexec_mount(noexec / "Games" / "PGR") == str(noexec)


def test_an_exec_folder_passes(noexec, tmp_path):
    assert execmount.noexec_mount(tmp_path / "good") is None


def test_an_unreadable_folder_is_not_blamed(monkeypatch, tmp_path):
    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(execmount.os, "statvfs", broken)
    assert execmount.noexec_mount(tmp_path) is None


def test_the_message_says_how_to_fix_it(noexec):
    with pytest.raises(BackendError) as caught:
        execmount.check(noexec / "Games")
    text = caught.value.message
    assert str(noexec) in text
    assert "noexec" in text
    assert "/etc/fstab" in text
    assert "exec" in text and "users" in text


def test_play_is_refused_when_the_install_drive_is_noexec(noexec, tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: None)
    log: list[str] = []
    backend = _Backend(noexec / "install", tmp_path / "prefix", log)
    with pytest.raises(BackendError, match="noexec"):
        launchgate.play(backend)
    assert log == []


def test_play_is_refused_when_the_prefix_drive_is_noexec(noexec, tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: None)
    backend = _Backend(tmp_path / "install", noexec / "prefix", [])
    with pytest.raises(BackendError, match="noexec"):
        launchgate.play(backend)


def test_play_goes_ahead_on_exec_drives(noexec, tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: None)
    log: list[str] = []
    assert launchgate.play(_Backend(tmp_path / "i", tmp_path / "p", log)) == 0
    assert log == ["launch"]


def test_a_backend_without_folders_is_not_checked():
    execmount.guard_launch(SimpleNamespace(kind="direct"))
    broken = SimpleNamespace(install_dir=lambda: 1 / 0, prefix=None)
    execmount.guard_launch(broken)  # a folder that cannot be told never blocks Play


def test_install_is_refused_on_a_noexec_drive(noexec):
    backend = SimpleNamespace(kind="", install_dir=lambda: noexec / "install")
    with pytest.raises(BackendError, match="noexec"):
        diskspace.install_space(backend)


def test_the_qt_install_preflight_refuses_too(noexec):
    from nadare.core import operation_policy

    backend = SimpleNamespace(kind="", install_dir=lambda: noexec / "install", install_size_hint=10)
    with pytest.raises(BackendError, match="noexec"):
        operation_policy.reserve_install_space(backend, "pgr")


def test_add_game_hint_warns(noexec, qtbot, monkeypatch):
    from nadare.gui import add_game

    hint = add_game.AddGameDialog._free_hint(None, str(noexec / "Games"))
    assert "noexec" in hint


def _wine(tmp_path, exe: Path, prefix: Path):
    from _backend_helpers import make_context

    from nadare.backends.wine import WineBackend

    return WineBackend(
        make_context(tmp_path, "wine", {"exe_name": str(exe), "prefix": str(prefix)})
    )


def test_a_hand_added_wine_game_on_a_noexec_drive_is_refused(noexec, tmp_path, monkeypatch):
    # The `wine` kind has no install_dir()/prefix(): the guard checks its exe folder (#1861).
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: None)
    exe = noexec / "Games" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    backend = _wine(tmp_path, exe, tmp_path / "prefix")
    launched: list[str] = []
    monkeypatch.setattr(backend, "launch", lambda: launched.append("launch") or 0)
    with pytest.raises(BackendError, match="noexec"):
        launchgate.play(backend)
    assert launched == []


def test_a_hand_added_wine_game_with_a_noexec_prefix_is_refused(noexec, tmp_path, monkeypatch):
    exe = tmp_path / "Games" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    with pytest.raises(BackendError, match="noexec"):
        execmount.guard_launch(_wine(tmp_path, exe, noexec / "prefix"))


def test_a_hand_added_wine_game_on_exec_drives_and_without_an_exe_is_let_through(noexec, tmp_path):
    exe = tmp_path / "Games" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    execmount.guard_launch(_wine(tmp_path, exe, tmp_path / "prefix"))
    # No exe set yet: the launch itself says so; the guard does not get in the way.
    from _backend_helpers import make_context

    from nadare.backends.wine import WineBackend

    execmount.guard_launch(WineBackend(make_context(tmp_path, "wine", {}, name="empty")))
