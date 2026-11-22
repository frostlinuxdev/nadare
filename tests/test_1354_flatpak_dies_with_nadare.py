# SPDX-License-Identifier: GPL-3.0-or-later
"""#1354: a Flatpak-Sandboxed game ends when the Nadare that started it is killed.

``flatpak-spawn --watch-bus`` ends the game only when ``flatpak-spawn`` itself goes away. A
killed Nadare left it running as an orphan, so the game kept running where no new Nadare could
see it ("stop" said "not running"). Measured on Bazzite 44 (2026-11-14): with a parent-death
signal on ``flatpak-spawn`` the sandboxed child ended with its killed parent; without it, it
stayed. The AppImage Sandbox has the same rule through bubblewrap's ``--die-with-parent``.
Closing the window is not killing Nadare: it hides and waits for the game (#20).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from nadare.core import gamesandbox


def _layout(tmp_path: Path) -> dict[str, Path]:
    prefix = tmp_path / "games" / "bd2" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    env_dir = tmp_path / "run"
    env_dir.mkdir()
    return {"prefix": prefix, "env_dir": env_dir}


def test_the_flatpak_command_starts_with_the_parent_death_helper(tmp_path):
    layout = _layout(tmp_path)
    command, _env = gamesandbox.wrap_flatpak(
        ["wine", "game.exe"],
        {},
        prefix=layout["prefix"],
        home=tmp_path,
        env_dir=layout["env_dir"],
    )
    helper = command[: command.index("/bin/sh")]
    # ``-m``, never ``-c``: the frozen binary has no ``-c`` (it read it as --config, so no
    # Sandboxed game started in the Flatpak); its entry runs ``-m`` modules itself.
    assert helper[:3] == [sys.executable, "-m", "nadare.core.diewithparent"]
    assert helper[3] == str(os.getpid())
    assert "flatpak-spawn" in command[command.index("/bin/sh") :]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie counts as gone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_the_child_dies_when_its_parent_is_killed(tmp_path):
    """A stand-in Nadare starts ``sleep`` through the helper, then is SIGKILLed."""
    pid_file = tmp_path / "child.pid"
    parent_code = textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        sys.path.insert(0, {str(Path(gamesandbox.__file__).parents[2])!r})
        from nadare.core import gamesandbox
        cmd = gamesandbox.die_with_parent(["sleep", "60"])
        child = subprocess.Popen(cmd, start_new_session=True)
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(60)
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # the helper has set the signal and exec'd into sleep
        child = int(pid_file.read_text())
        assert _alive(child)
        parent.send_signal(signal.SIGKILL)
        parent.wait(5)
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "the child outlived its killed parent"
    finally:
        if parent.poll() is None:
            parent.kill()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_a_parent_already_gone_means_no_child(tmp_path):
    """The helper checks its parent after setting the signal: no orphan from a race."""
    out = subprocess.run(
        [*gamesandbox.die_with_parent(["echo", "ran"], parent_pid=1)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode != 0 and "ran" not in out.stdout


def test_the_helper_passes_arguments_through_unchanged():
    out = subprocess.run(
        gamesandbox.die_with_parent(
            [sys.executable, "-c", "import sys; print(sys.argv[1:])", "a b", "$x"]
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.stdout.strip() == "['a b', '$x']"


ENTRY = Path(__file__).resolve().parents[1] / "packaging" / "nadare_entry.py"
SRC = Path(__file__).resolve().parents[1] / "src"


def _through_entry(argv, **kwargs):
    """Run a helper command the way the frozen binary does: through its entry script."""
    assert argv[0] == sys.executable
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.update(kwargs.pop("env", {}))
    return subprocess.run(
        [sys.executable, str(ENTRY), *argv[1:]], capture_output=True, text=True,
        timeout=20, env=env, **kwargs,
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_the_frozen_entry_runs_the_helper():
    out = _through_entry(gamesandbox.die_with_parent(["echo", "ran"]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ran"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_the_frozen_entry_refuses_a_gone_parent():
    out = _through_entry(gamesandbox.die_with_parent(["echo", "ran"], parent_pid=1))
    assert out.returncode != 0 and "ran" not in out.stdout


def test_the_helper_module_is_bundled():
    """PyInstaller collects what is imported: gamesandbox imports the helper module."""
    from nadare.core import diewithparent

    assert gamesandbox.diewithparent is diewithparent
