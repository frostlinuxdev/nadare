# SPDX-License-Identifier: GPL-3.0-or-later
"""Two CLI shortcut additions must not overwrite each other's Steam entry."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import nadare
from nadare.core import shortcuts

_CHILD = r"""
import sys, time
from pathlib import Path
from nadare.core import shortcuts

ready, go, vdf = map(Path, sys.argv[1:4])
shortcuts.steam_running = lambda: False
original = shortcuts.vdf_loads

def paused(data, *, strict=False):
    result = original(data, strict=strict)
    ready.write_text("read")
    deadline = time.monotonic() + 10
    while not go.exists():
        if time.monotonic() > deadline:
            raise SystemExit("parent did not signal")
        time.sleep(0.01)
    return result

shortcuts.vdf_loads = paused
shortcuts.add_steam_shortcut(vdf, "two", "Game Two", ["/opt/two"])
"""


def test_concurrent_steam_shortcut_additions_keep_both(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    vdf = tmp_path / "shortcuts.vdf"
    vdf.write_bytes(shortcuts.vdf_dumps([]))
    ready, go = tmp_path / "child-read", tmp_path / "child-go"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(ready), str(go), str(vdf)],
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the second process did not read the shortcut file")
            time.sleep(0.01)

        # Both processes read the same empty file; this one writes first.
        assert shortcuts.add_steam_shortcut(vdf, "one", "Game One", ["/opt/one"]) is True
        go.write_text("go")
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    entries = shortcuts.vdf_loads(vdf.read_bytes(), strict=True)
    profiles = sorted(
        shortcuts._steam_shortcut_profile(entry) for entry in entries
    )
    print("Steam entries on disk:", profiles)
    print("expected:", ["one", "two"])
    assert profiles == ["one", "two"], "one process overwrote the other's Steam shortcut"
