# SPDX-License-Identifier: GPL-3.0-or-later
"""A FIFO where shortcuts.vdf should be must not freeze the CLI forever."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import nadare
from nadare.core.config import Config


def test_cli_adding_a_steam_shortcut_does_not_hang_on_a_fifo(tmp_path):
    vdf = (
        Path(os.environ["HOME"])
        / ".local"
        / "share"
        / "Steam"
        / "userdata"
        / "42"
        / "config"
        / "shortcuts.vdf"
    )
    vdf.parent.mkdir(parents=True)
    os.mkfifo(vdf)

    cfg = Config.load()
    cfg.add_profile("game", backend="wine", title="Game")
    cfg.save()

    source = str(Path(nadare.__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        done = subprocess.run(
            [sys.executable, "-m", "nadare", "shortcut", "add", "-p", "game", "--steam"],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "`nadare shortcut add --steam` blocked forever reading a FIFO as "
            "shortcuts.vdf (killed after 8s); it should report a bad file instead"
        )

    print("return code:", done.returncode)
    print("stdout:", done.stdout[:200])
    print("stderr:", done.stderr[:400])
    assert "Traceback" not in done.stderr
