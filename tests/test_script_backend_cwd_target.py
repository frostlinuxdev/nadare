# SPDX-License-Identifier: GPL-3.0-or-later
"""A script runs in its own folder, and a .bat runs the file the backend found (#862)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _timing import scaled

from nadare.backends import script as script_mod
from nadare.backends import wine as wine_mod


def _backend(tmp_path: Path, profile: dict):
    return script_mod.ScriptBackend(make_context(tmp_path, "script", profile))


def test_a_script_runs_in_its_own_folder_whatever_nadares_cwd_is(tmp_path, monkeypatch):
    game = tmp_path / "game"
    write_exe(game / "run.sh", "#!/bin/sh\ntest -f game-data.bin\n")
    (game / "game-data.bin").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    backend = _backend(tmp_path, {"launch_exe": "run.sh", "install_dir": str(game)})
    assert backend.launch() == 0


def test_a_script_in_a_subfolder_runs_in_that_subfolder(tmp_path, monkeypatch):
    game = tmp_path / "game"
    write_exe(game / "bin" / "run.sh", "#!/bin/sh\ntest -f here.txt\n")
    (game / "bin" / "here.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    backend = _backend(tmp_path, {"launch_exe": "bin/run.sh", "install_dir": str(game)})
    assert backend.launch() == 0


def test_the_working_directory_is_passed_to_stream(tmp_path, monkeypatch):
    game = tmp_path / "game"
    write_exe(game / "run.sh")
    seen = {}

    def fake_stream(argv, env, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(script_mod, "stream", fake_stream)
    _backend(tmp_path, {"launch_exe": "run.sh", "install_dir": str(game)}).launch()
    assert seen["cwd"] == str(game)


@pytest.fixture
def both_names(tmp_path):
    game = tmp_path / "game"
    (game).mkdir()
    (game / "new.bat").write_text("@echo off\n")
    (game / "old.exe").write_bytes(b"MZ")
    return _backend(
        tmp_path, {"launch_exe": "new.bat", "exe_name": "old.exe", "install_dir": str(game)}
    ), game


def test_a_batch_profile_with_a_stale_exe_name_runs_the_batch_file(both_names, monkeypatch):
    backend, game = both_names
    ran = []
    monkeypatch.setattr(
        wine_mod.WineBackend, "launch", lambda self: ran.append(self._target()) or 0
    )
    assert backend._is_batch()
    backend.launch()
    assert ran == [game / "new.bat"]


def test_stop_looks_for_the_same_batch_file(both_names, monkeypatch):
    backend, game = both_names
    asked = []
    monkeypatch.setattr(wine_mod.WineBackend, "stop", lambda self: asked.append(self._target()))
    backend.stop()
    assert asked == [game / "new.bat"]


def test_a_plain_wine_profile_still_prefers_exe_name(tmp_path):
    ctx = make_context(tmp_path, "wine", {"exe_name": "game.exe", "launch_exe": "other.exe"})
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.exe").write_bytes(b"MZ")
    (game / "other.exe").write_bytes(b"MZ")
    ctx.profile["install_dir"] = str(game)
    assert wine_mod.WineBackend(ctx)._target() == game / "game.exe"


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is gone as far as the game is concerned
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


def test_stop_from_another_command_also_stops_what_the_script_started(tmp_path):
    """#932: stop used to signal only the shell running the script, leaving the game behind."""
    import os
    import subprocess
    import time

    game = tmp_path / "game"
    child_pid = tmp_path / "child.pid"
    write_exe(game / "run.sh", f"#!/bin/bash\nsleep 300 &\necho $! > {child_pid}\nwait\n")
    backend = _backend(tmp_path, {"launch_exe": "run.sh", "install_dir": str(game)})
    proc = subprocess.Popen(["bash", str(game / "run.sh")], start_new_session=True)
    try:
        for _ in range(100):
            if child_pid.exists() and child_pid.read_text().strip():
                break
            time.sleep(0.05)
        child = int(child_pid.read_text())
        assert _alive(child)

        backend.stop()  # as a separate `nadare stop` would: nothing here owns the process

        for _ in range(int(scaled(100))):
            if not _alive(child):
                break
            time.sleep(0.05)
        assert not _alive(child), "the script's child was left running"
    finally:
        for pid in (proc.pid,):
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
        proc.wait()
