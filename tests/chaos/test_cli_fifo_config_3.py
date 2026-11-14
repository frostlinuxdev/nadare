# SPDX-License-Identifier: GPL-3.0-or-later
"""A FIFO passed to ``-c`` must not make the CLI wait forever."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import nadare


def test_cli_does_not_hang_on_a_fifo_config(tmp_path):
    config = tmp_path / "config.json"
    os.mkfifo(config)

    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"

    try:
        done = subprocess.run(
            [sys.executable, "-m", "nadare", "-c", str(config), "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "the CLI blocked forever reading a FIFO given to -c "
            "(killed after 5s; a special file should be rejected or read non-blockingly)"
        )

    print("return code:", done.returncode)
    print("stdout:", done.stdout[:120])
    print("stderr:", done.stderr[:300])
    assert "Traceback" not in done.stderr, "the CLI crashed instead of reporting a bad config"
