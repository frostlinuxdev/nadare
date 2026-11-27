# SPDX-License-Identifier: GPL-3.0-or-later
"""#1354 (rig, 2026-11-15): a Flatpak-Sandboxed Wine game outlived a killed Nadare.

``--watch-bus`` signals only the sandbox's top process; with ``--share-pids`` Wine's processes
live in Nadare's own PID namespace and kept running (BD2 on a test rig, Flatpak 1.16.6,
portal 1.20.3; a plain ``sleep`` did end, which is why the first test passed). The sandboxed
wrapper now watches the Nadare process that started it (PID + start time, so a reused PID never
counts) and runs ``wineserver -k`` for the game's prefix when it is gone. It arms only when that
process is visible at the start, so a Flatpak without shared PIDs keeps today's behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nadare.core import gamesandbox

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")


def _calls(tmp_path: Path) -> list[str]:
    """The flag of each fake wineserver call (``-k`` or ``-w``: the last word of its log line).

    A substring check on the whole log matched "-k" inside a path such as
    ``aurora-wt-...-keyart``; compare the flags instead.
    """
    lines = (tmp_path / "wineserver.log").read_text().splitlines()
    return [line.rsplit(" ", 1)[-1] for line in lines]


def _fake_wineserver(tmp_path: Path) -> Path:
    log = tmp_path / "wineserver.log"
    killed = tmp_path / "killed"
    script = tmp_path / "wineserver"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$WINEPREFIX $*" >> {log}\n'
        f'[ "$1" = "-k" ] && touch {killed}\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


def _game(tmp_path: Path) -> list[str]:
    """A stand-in game: runs until wineserver -k ends it (or 30 s), then exits 7."""
    killed = tmp_path / "killed"
    return ["/bin/sh", "-c", f"i=0; while [ ! -e {killed} ] && [ $i -lt 300 ]; do sleep 0.1; "
            "i=$((i+1)); done; exit 7"]


def _wrapper(tmp_path: Path, parent_pid: int, start: str, program: list[str]):
    wineserver = _fake_wineserver(tmp_path)
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(tmp_path / "prefix"))
    argv = ["/bin/sh", "-c", gamesandbox._WAIT_FOR_WINE, "nadare-wait",  # noqa: SLF001
            str(wineserver), str(parent_pid), start, *program]
    return subprocess.Popen(argv, env=env)


def test_the_game_is_ended_when_nadare_dies(tmp_path):
    nadare = subprocess.Popen(["sleep", "60"])
    try:
        start = gamesandbox.process_start_time(nadare.pid)
        assert start
        wrapper = _wrapper(tmp_path, nadare.pid, start, _game(tmp_path))
        time.sleep(1)
        assert wrapper.poll() is None
        nadare.kill()
        nadare.wait()
        assert wrapper.wait(timeout=15) == 7
        log = (tmp_path / "wineserver.log").read_text()
        assert f"{tmp_path / 'prefix'}/pfx -k" in log
    finally:
        nadare.kill()


def test_a_living_nadare_never_ends_the_game(tmp_path):
    wrapper = _wrapper(tmp_path, os.getpid(), gamesandbox.process_start_time(os.getpid()),
                       ["/bin/sh", "-c", "sleep 3; exit 3"])
    assert wrapper.wait(timeout=20) == 3
    calls = _calls(tmp_path)
    assert "-k" not in calls and "-w" in calls


def test_not_armed_when_nadare_is_not_visible(tmp_path):
    """A wrong start time (another process, or a PID namespace Nadare is not in): no -k."""
    wrapper = _wrapper(tmp_path, os.getpid(), "1", ["/bin/sh", "-c", "sleep 3; exit 3"])
    assert wrapper.wait(timeout=20) == 3
    assert "-k" not in _calls(tmp_path)


def test_a_reused_pid_does_not_keep_the_game_alive(tmp_path):
    """Only the very process counts: a dead Nadare whose PID is reused still ends the game."""
    nadare = subprocess.Popen(["sleep", "60"])
    start = gamesandbox.process_start_time(nadare.pid)
    wrapper = _wrapper(tmp_path, nadare.pid, start, _game(tmp_path))
    time.sleep(1)
    nadare.kill()
    nadare.wait()
    assert wrapper.wait(timeout=15) == 7


def test_the_flatpak_command_passes_nadare_pid_and_start(tmp_path):
    prefix = tmp_path / "games" / "bd2" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    runner = tmp_path / "runner"
    (runner / "files" / "bin").mkdir(parents=True)
    (runner / "files" / "bin" / "wineserver").write_text("#!/bin/sh\n")
    (runner / "proton").write_text("")
    env_dir = tmp_path / "run"
    env_dir.mkdir()
    command, _env = gamesandbox.wrap_flatpak(
        [str(runner / "proton"), "run", "game.exe"], {},
        prefix=prefix, runner_dir=runner, home=tmp_path, env_dir=env_dir,
    )
    i = command.index("nadare-wait")
    assert command[i + 2] == str(os.getpid())
    assert command[i + 3] == gamesandbox.process_start_time(os.getpid())


def test_another_process_with_that_pid_is_not_nadare(tmp_path):
    """The PID belongs to a process with another start time: never watched, never ends the game."""
    other = subprocess.Popen(["sleep", "60"])
    try:
        wrapper = _wrapper(tmp_path, other.pid, "1", ["/bin/sh", "-c", "sleep 4; exit 3"])
        time.sleep(1)
        other.kill()
        other.wait()
        assert wrapper.wait(timeout=20) == 3
        assert "-k" not in _calls(tmp_path)
    finally:
        other.kill()
