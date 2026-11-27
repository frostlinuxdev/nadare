# SPDX-License-Identifier: GPL-3.0-or-later
"""Real-bwrap tests say why they cannot run from a checkout under a hidden mount (#1751)."""

from pathlib import Path

import test_sandbox_tmp_pid_1593 as t


def test_a_python_under_mnt_is_named():
    reason = t._hidden_by_the_sandbox(["/mnt/data/x/.venv/bin/python", "/home/u/repo"])
    assert reason is not None
    assert "/mnt/data/x/.venv/bin/python" in reason and "/mnt" in reason


def test_media_and_run_media_count_too():
    assert t._hidden_by_the_sandbox(["/run/media/u/disk/repo"]) is not None
    assert t._hidden_by_the_sandbox(["/media/disk/py"]) is not None


def test_a_home_checkout_is_fine():
    assert t._hidden_by_the_sandbox(["/home/u/repo/.venv/bin/python", "/usr"]) is None


def test_a_lookalike_prefix_is_not_a_mount():
    assert t._hidden_by_the_sandbox(["/mntx/repo", "/mediafoo"]) is None


def test_the_checked_paths_cover_the_interpreter_and_the_repo():
    paths = t._paths_the_sandbox_must_see()
    import sys

    assert sys.executable in paths and str(Path(t.__file__).resolve().parent.parent) in paths
