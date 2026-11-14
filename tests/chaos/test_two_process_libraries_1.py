# SPDX-License-Identifier: GPL-3.0-or-later
"""Two Nadare processes adding different libraries must not drop one of them."""

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


def test_concurrent_library_additions_keep_both(tmp_path):
    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    one = tmp_path / "libraries" / "one"
    two = tmp_path / "libraries" / "two"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    config_path = paths.config_file()
    config = Config.load(config_path)
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
            str(two),
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the second process did not load the config")
            time.sleep(0.01)

        # Both processes loaded the same (empty) library list before either saved.
        libraries.add_library(config, one)
        config.save()
        go.write_text("go")
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    kept = _stored(config_path)
    expected = {str(one.resolve()), str(two.resolve())}
    print("libraries on disk:", kept)
    print("expected:", sorted(expected))
    assert set(kept) == expected, "a library added by the other process was lost"
