# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: an import move killed between its two trees is recoverable.

``importmove.run`` moves the game folder and then the prefix.  It rolls back when
Python sees an exception, but not when the process dies (SIGKILL/power loss).  A
kill after the first move leaves the install under the new library and the prefix
at the old place.  On the next start ``plan`` refuses with 'game folder not
found', so there is no way to finish the interrupted move from the app.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nadare.core import importmove
from nadare.core.config import Config

REPO = Path(__file__).resolve().parents[2]


def test_a_killed_import_move_can_be_finished_on_the_next_run(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = Config.load(cfg_path)
    old = tmp_path / "old-games"
    install = old / "Game"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "game.pak").write_bytes(b"pak" * 1000)
    prefix = old / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    cfg.add_profile(
        "g",
        backend="direct-wuwa",
        title="Game",
        install_dir=str(install),
        prefix=str(prefix),
    )
    cfg.save()
    library = tmp_path / "Games"
    library.mkdir()

    marker = tmp_path / "first-tree-moved.marker"
    child_code = f"""
import time
from pathlib import Path
from nadare.core import importmove, relocate
from nadare.core.config import Config
real = relocate.move_tree
def hook(src, dst, **kwargs):
    result = real(src, dst, **kwargs)
    if Path(src) == Path({str(install)!r}):
        Path({str(marker)!r}).write_text("install moved")
        time.sleep(60)
    return result
relocate.move_tree = hook
cfg = Config.load(Path({str(cfg_path)!r}))
move = importmove.plan(cfg, "g", Path({str(library)!r}))
importmove.run(move)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists() and child.poll() is None:
            time.sleep(0.02)
        if not marker.exists():
            out, err = child.communicate(timeout=5)
            pytest.fail(f"child never moved the first tree:\n{out}\n{err}")
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    home = library / "Nadare" / "Game"
    print("old install exists:", install.exists())
    print("old prefix exists:", prefix.exists())
    print("new install exists:", (home / "install").exists())
    print("new prefix exists:", (home / "prefix").exists())
    assert not install.exists() and (home / "install").is_dir()
    assert prefix.is_dir() and not (home / "prefix").exists()

    after_kill = Config.load(cfg_path)
    move = importmove.plan(after_kill, "g", library)  # recover the half-done move
    importmove.run(move)
    print("after recovery:", sorted(p.name for p in home.iterdir()))
    assert (home / "install" / "Client" / "game.pak").is_file()
    assert (home / "prefix" / "drive_c").is_dir()
