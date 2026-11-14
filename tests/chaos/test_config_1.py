# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: a SIGKILL during save must not litter the config folder.

The target config is intact (that part is correct), but the temporary file the
killed save was writing is never swept by the next load/save.  A few crashed
starts leave a pile of ``.config-*.tmp`` files next to the user's config.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nadare.core.config import Config

REPO = Path(__file__).resolve().parents[2]


def test_a_killed_save_leaves_no_temporary_file_behind(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = Config.load(cfg_path)
    cfg.data["language"] = "en"
    cfg.save()
    before = json.loads(cfg_path.read_text(encoding="utf-8"))

    marker = tmp_path / "saving.marker"
    child_code = f"""
import os, time
from pathlib import Path
from nadare.core import config
real = os.replace
def hook(src, dst):
    Path({str(marker)!r}).write_text("about to replace")
    time.sleep(60)
    return real(src, dst)
config.os.replace = hook
cfg = config.Config.load(Path({str(cfg_path)!r}))
cfg.data["language"] = "de"
cfg.save()
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
            pytest.fail(f"child never reached the save rename:\n{out}\n{err}")
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    # The real config was not replaced, which is right.
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == before
    leftovers = sorted(p.name for p in tmp_path.glob(".config-*.tmp"))
    print("temporary files after the kill:", leftovers)
    assert leftovers, "the fixture did not leave a save temporary behind"

    # A later normal start must clean up the crashed save's file.
    again = Config.load(cfg_path)
    again.save()
    leftovers = sorted(p.name for p in tmp_path.glob(".config-*.tmp"))
    print("temporary files after the next save:", leftovers)
    assert leftovers == []
