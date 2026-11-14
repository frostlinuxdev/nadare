# SPDX-License-Identifier: GPL-3.0-or-later
"""An addition in one process must not bring back a library removed in another."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import nadare
from nadare.core import libraries, paths
from nadare.core.config import Config

_CHILD = r"""
import sys, time
from pathlib import Path
from nadare.core import libraries
from nadare.core.config import Config

config_path, ready, go, library = map(Path, sys.argv[1:5])
config = Config.load(config_path)
ready.write_text("ready")
deadline = time.monotonic() + 10
while not go.exists():
    if time.monotonic() > deadline:
        raise SystemExit("parent did not signal")
    time.sleep(0.01)
libraries.add_library(config, library)
config.save()
"""


def _stored(config_path: Path) -> list[str]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return [entry.get("path") for entry in raw.get("libraries") or []]


def test_a_removed_library_stays_removed_when_another_process_adds_one(tmp_path):
    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    old = tmp_path / "libraries" / "old"
    new = tmp_path / "libraries" / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    config_path = paths.config_file()
    config = Config.load(config_path)
    libraries.add_library(config, old)
    config.save()

    ready, go = tmp_path / "child-ready", tmp_path / "child-go"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(config_path),
            str(ready),
            str(go),
            str(new),
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the second process did not load the config")
            time.sleep(0.01)

        # Both loaded [old]; this process removes it while the other adds "new".
        assert libraries.remove_library(config, old) is True
        config.save()
        go.write_text("go")
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    kept = _stored(config_path)
    print("libraries on disk:", kept)
    print("removed library:", old.resolve())
    assert str(old.resolve()) not in kept, "a removed library came back"
    assert str(new.resolve()) in kept, "the other process's new library was lost"
