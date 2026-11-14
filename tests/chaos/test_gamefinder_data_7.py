# SPDX-License-Identifier: GPL-3.0-or-later
"""A state file that is a FIFO must be treated as unreadable, not block the process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_version_reader_does_not_hang_on_a_fifo(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    os.mkfifo(game / ".nadare-version.json")

    src = Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    code = (
        "from pathlib import Path;"
        "from nadare.core import gamefinder;"
        f"print(gamefinder._read_version(Path({str(game)!r})))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=2,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "reading a FIFO .nadare-version.json hung instead of treating it as unreadable"
        )

    print("return code:", result.returncode)
    print("stdout:", result.stdout)
    print("stderr:", result.stderr[-500:])
    assert result.returncode == 0
    assert result.stdout.strip() == "None"
