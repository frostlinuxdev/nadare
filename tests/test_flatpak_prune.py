# SPDX-License-Identifier: GPL-3.0-or-later
"""prune.sh keeps the last N releases in the Flatpak repo and in the bucket, deleting last."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "flatpak" / "prune.sh"


def _fake(bin_dir: Path, name: str, log: Path) -> None:
    tool = bin_dir / name
    tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def setup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake(bin_dir, "flatpak", log)
    _fake(bin_dir, "rclone", log)
    repo = tmp_path / "repo"
    (repo / "objects").mkdir(parents=True)
    (repo / "refs").mkdir()
    for name in ("summary", "nadare.flatpakref", "index.flatpakrepo", "config"):
        (repo / name).write_text("x")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RCLONE=str(bin_dir / "rclone"))
    return repo, log, env


def _run(args, env):
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_prunes_locally_then_uploads_then_deletes_in_the_bucket(setup):
    repo, log, env = setup
    result = _run([str(repo), "3", "--gpg-sign=KEY"], env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    prune = next(i for i, c in enumerate(calls) if c.startswith("flatpak build-update-repo"))
    assert "--prune" in calls[prune] and "--prune-depth=2" in calls[prune]
    assert "--gpg-sign=KEY" in calls[prune]  # the summary is re-signed
    summary = next(
        i for i, c in enumerate(calls) if "copyto" in c and c.split()[-1].endswith("/summary")
    )
    syncs = [i for i, c in enumerate(calls) if c.startswith("rclone sync")]
    assert syncs, calls
    assert prune < summary < min(syncs)  # nothing is deleted before the new summary is up
    assert any(calls[i].split()[-1].endswith("/objects") for i in syncs)
    assert not any(c.startswith("rclone sync") and "/refs" in c for c in calls)


def test_refuses_to_keep_fewer_than_two(setup):
    repo, log, env = setup
    result = _run([str(repo), "1"], env)
    assert result.returncode != 0
    assert not log.exists()


def test_refuses_a_directory_that_is_not_a_built_repo(setup, tmp_path):
    _, log, env = setup
    result = _run([str(tmp_path / "nothing"), "3"], env)
    assert result.returncode != 0
    assert not log.exists()
