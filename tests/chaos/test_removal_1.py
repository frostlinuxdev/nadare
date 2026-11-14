# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: a Remove killed after deleting the prefix can be finished.

The child process really deletes the prefix and is then SIGKILLed while the
shortcut step runs, so the profile is still in the config.  Re-running the same
Remove must not keep the profile forever just because the requested folder is
already gone: there is nothing left to delete, so the removal is complete.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nadare.core import removal
from nadare.core.config import Config

REPO = Path(__file__).resolve().parents[2]


def _wait(path: Path, child: subprocess.Popen, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        if child.poll() is not None:
            break
        time.sleep(0.02)
    out, err = child.communicate(timeout=5)
    pytest.fail(f"child never reached the delete step:\n{out}\n{err}")


def test_a_remove_killed_after_the_prefix_was_deleted_can_be_finished(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = Config.load(cfg_path)
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "game.exe").write_text("MZ")
    cfg.profiles["gone"].update(prefix=str(prefix), owned_trees=["prefix"])
    cfg.save()

    marker = tmp_path / "deleted.marker"
    child_code = f"""
import sys, time
from pathlib import Path
from nadare.core import removal
from nadare.core.config import Config
real = removal._rmtree_safely
def hook(path, keep=()):
    result = real(path, keep)
    Path({str(marker)!r}).write_text("deleted")
    time.sleep(60)
    return result
removal._rmtree_safely = hook
cfg = Config.load(Path({str(cfg_path)!r}))
removal.remove_game(
    cfg, "gone", delete_trees=["prefix"],
    applications_dir=Path({str(tmp_path / 'apps')!r}),
)
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
        _wait(marker, child)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    assert not prefix.exists(), "the fixture did not delete the prefix before the kill"
    after_kill = Config.load(cfg_path)
    print("profile after the kill:", "gone" in after_kill.profiles)
    assert "gone" in after_kill.profiles, "the fixture must leave the interrupted profile"

    # The user repeats the same Remove command.  The requested folder is gone,
    # so all the deletion work is done; only the profile is left.
    result = removal.remove_game(
        after_kill,
        "gone",
        delete_trees=["prefix"],
        applications_dir=tmp_path / "apps",
    )
    print("retry warnings:", result.warnings)
    print("retry profile_kept:", result.profile_kept)
    print("profile after the retry:", "gone" in after_kill.profiles)
    assert "gone" not in after_kill.profiles
    assert "gone" not in Config.load(cfg_path).profiles
