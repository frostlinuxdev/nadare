# SPDX-License-Identifier: GPL-3.0-or-later
"""#974 item 2: the old runner set aside during a swap is owned from its first moment.

An install renamed the working runner to ``.<name>.old-*`` and only then wrote its owner marker.
A concurrent installer pruning leftovers in that gap deleted the backup, so when the new tree
could not be published the rollback found nothing and the runner was gone.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from nadare.core import runners


def _runner_tar(tmp_path: Path, name: str, content: str) -> Path:
    src = tmp_path / f"src-{content}" / name
    src.mkdir(parents=True)
    (src / "proton").write_text("#!/bin/sh\n")
    (src / "version").write_text(content)
    tar_path = tmp_path / f"{name}-{content}.tar"
    with tarfile.open(tar_path, "w") as archive:
        archive.add(src, arcname=name)
    return tar_path


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    return folder


def test_a_prune_during_the_swap_cannot_take_the_backup(tmp_path, root, monkeypatch):
    runners._extract(_runner_tar(tmp_path, "GE-X", "old"), cancel=None, emit=lambda _e: None)
    assert (root / "GE-X" / "version").read_text() == "old"

    real_replace = runners.os.replace
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if Path(dst).name.startswith(".GE-X.old-"):
            real_replace(src, dst)
            runners._prune_stale(root)  # another installer prunes right now
            return
        if Path(dst) == root / "GE-X" and not Path(src).name.startswith(".GE-X.old-"):
            raise OSError("disk full")  # the new tree cannot be published (rollback may)
        real_replace(src, dst)

    monkeypatch.setattr(runners.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        runners._extract(_runner_tar(tmp_path, "GE-X", "new"), cancel=None, emit=lambda _e: None)
    monkeypatch.setattr(runners.os, "replace", real_replace)

    assert (root / "GE-X" / "version").read_text() == "old"  # rolled back, not lost
    assert not (root / "GE-X" / runners._OWNER_MARKER).exists()
    assert not list(root.glob(".GE-X.old-*"))


def test_a_normal_swap_leaves_no_marker_or_backup(tmp_path, root):
    runners._extract(_runner_tar(tmp_path, "GE-X", "old"), cancel=None, emit=lambda _e: None)
    runners._extract(_runner_tar(tmp_path, "GE-X", "new"), cancel=None, emit=lambda _e: None)
    assert (root / "GE-X" / "version").read_text() == "new"
    assert not (root / "GE-X" / runners._OWNER_MARKER).exists()
    assert not list(root.glob(".GE-X.old-*"))


def test_a_backup_left_by_a_dead_installer_is_still_pruned(root):
    leftover = root / ".GE-X.old-dead"
    leftover.mkdir()
    (leftover / runners._OWNER_MARKER).write_text("999999999")
    runners._prune_stale(root)
    assert not leftover.exists()
