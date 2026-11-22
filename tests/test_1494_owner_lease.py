# SPDX-License-Identifier: GPL-3.0-or-later
"""Runner staging and swap backups carry a namespace-stable lease (#1494).

A PID in ``.nadare-owner-pid`` means nothing in another PID namespace (a Flatpak next to an
AppImage, or any sandbox): cleanup there sees the live owner as dead. The owner now holds a
``flock`` on the marker, which every namespace on the same filesystem sees. Synthetic folders
and tiny synthetic tarballs only; real PID namespaces through ``unshare``.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from nadare.core import leftovers, ownerlease, runners

SRC = Path(runners.__file__).resolve().parents[2]
DEAD_PID = 999_999_999


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for key, leaf in (("HOME", "home"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                      ("XDG_CONFIG_HOME", "config")):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    monkeypatch.delenv("NADARE_CHANNEL", raising=False)


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _namespace_prune(root: Path, mode: str) -> None:
    child = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from nadare.core import runners, leftovers
root = pathlib.Path(sys.argv[2])
if sys.argv[3] == 'runners':
    runners._prune_stale(root)
else:
    leftovers._sweep_unpacks(root, time.time(), leftovers.MIN_AGE, leftovers.Result())
print('pruned', flush=True)
"""
    result = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
         sys.executable, "-c", child, str(SRC), str(root), mode],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode and ("Operation not permitted" in result.stderr
                              or "No such file" in result.stderr):
        pytest.skip("PID namespaces are not available here")
    assert result.returncode == 0 and "pruned" in result.stdout, result.stderr


def _staging(root: Path) -> tuple[Path, Path]:
    staging = root / ".unpack-synthetic"
    staging.mkdir(parents=True)
    data = staging / "in-progress.bin"
    data.write_text("synthetic active unpack data")
    return staging, data


@pytest.mark.parametrize("mode", ["runners", "leftovers"])
def test_a_live_unpack_survives_cleanup_in_another_pid_namespace(tmp_path, mode):
    root = tmp_path / "runners"
    staging, data = _staging(root)
    lease = ownerlease.take(staging)
    try:
        _age(staging, 2 * 86400)
        _namespace_prune(root, mode)
        assert data.exists(), "another PID namespace took the live owner for dead"
    finally:
        lease.release()


@pytest.mark.parametrize("mode", ["runners", "leftovers"])
def test_an_old_unpack_is_cleaned_once_its_owner_let_go(tmp_path, mode):
    root = tmp_path / "runners"
    staging, _ = _staging(root)
    ownerlease.take(staging).release()
    # The owner's PID may well be alive here (it is this test); in another namespace it is not.
    (staging / ownerlease.MARKER).write_text(f"{DEAD_PID} lease\n")
    _age(staging, 2 * 86400)
    _age(staging / ownerlease.MARKER, 2 * 86400)
    _namespace_prune(root, mode)
    assert not staging.exists()


def test_a_young_unpack_is_kept_even_without_an_owner(tmp_path):
    root = tmp_path / "runners"
    staging, data = _staging(root)
    runners._prune_stale(root)
    leftovers._sweep_unpacks(root, time.time(), leftovers.MIN_AGE, leftovers.Result())
    assert data.exists()


def _tar(tmp_path: Path, version: str) -> Path:
    source = tmp_path / f"source-{version}" / "SyntheticRunner"
    source.mkdir(parents=True)
    (source / "proton").write_text("#!/bin/sh\n")
    (source / "version").write_text(version)
    path = tmp_path / f"{version}.tar"
    with tarfile.open(path, "w") as archive:
        archive.add(source, arcname="SyntheticRunner")
    return path


@pytest.mark.parametrize("separate_namespace", [True, False], ids=["namespace", "control"])
def test_the_swap_backup_survives_a_prune_for_rollback(tmp_path, monkeypatch, separate_namespace):
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    runners._extract(_tar(tmp_path, "old"), cancel=None, emit=lambda _e: None)
    real_replace = os.replace

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        if destination.name.startswith(".SyntheticRunner.old-"):
            real_replace(source, destination)
            assert "lease" in (destination / ownerlease.MARKER).read_text()
            if separate_namespace:
                _namespace_prune(root, "runners")
            else:
                runners._prune_stale(root)
            return
        if destination == root / "SyntheticRunner" and not source.name.startswith(
            ".SyntheticRunner.old-"
        ):
            raise OSError("synthetic publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(runners.os, "replace", replace)
    with pytest.raises(OSError, match="synthetic publish failure"):
        runners._extract(_tar(tmp_path, "new"), cancel=None, emit=lambda _e: None)
    monkeypatch.setattr(runners.os, "replace", real_replace)
    assert (root / "SyntheticRunner" / "version").read_text() == "old"
    assert not (root / "SyntheticRunner" / ownerlease.MARKER).exists()
    assert not list(root.glob(".SyntheticRunner.old-*"))


def test_a_successful_swap_leaves_no_marker_and_no_backup(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    runners._extract(_tar(tmp_path, "old"), cancel=None, emit=lambda _e: None)
    runners._extract(_tar(tmp_path, "new"), cancel=None, emit=lambda _e: None)
    assert (root / "SyntheticRunner" / "version").read_text() == "new"
    assert not (root / "SyntheticRunner" / ownerlease.MARKER).exists()
    assert not list(root.glob(".SyntheticRunner.old-*"))
    assert not list(root.glob(".unpack-*"))


# --- unknown ownership: kept until old ----------------------------------------------------


def test_a_backup_with_a_bare_pid_marker_is_kept_while_young(tmp_path):
    backup = tmp_path / ".GE-X.old-legacy"
    backup.mkdir()
    (backup / ownerlease.MARKER).write_text(str(DEAD_PID))
    runners._prune_stale(tmp_path)
    assert backup.exists(), "a PID from another namespace proves nothing"
    _age(backup, 7 * 3600)
    _age(backup / ownerlease.MARKER, 7 * 3600)
    runners._prune_stale(tmp_path)
    assert not backup.exists()


def test_an_unreadable_marker_is_unknown_not_free(tmp_path):
    backup = tmp_path / ".GE-X.old-odd"
    backup.mkdir()
    (backup / ownerlease.MARKER).mkdir()  # not a file
    runners._prune_stale(tmp_path)
    assert backup.exists()


def test_a_marker_link_is_not_followed(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(f"{DEAD_PID} lease\n")  # would read as a let-go lease
    backup = tmp_path / ".GE-X.old-link"
    backup.mkdir()
    (backup / ownerlease.MARKER).symlink_to(elsewhere)
    assert ownerlease.may_remove(backup, unknown_min_age=0) is False


def test_a_young_backup_without_a_marker_is_kept(tmp_path):
    backup = tmp_path / ".GE-X.old-nomarker"
    backup.mkdir()
    runners._prune_stale(tmp_path)
    assert backup.exists()


def test_extract_holds_a_lease_on_its_own_staging(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    real_check = runners._looks_like_runner
    seen = []

    def check(folder):
        staging = next(root.glob(".unpack-*"))
        assert "lease" in (staging / ownerlease.MARKER).read_text()
        _age(staging, 2 * 86400)
        _age(staging / ownerlease.MARKER, 2 * 86400)
        _namespace_prune(root, "leftovers")
        _namespace_prune(root, "runners")
        seen.append(folder.exists())
        return real_check(folder)

    monkeypatch.setattr(runners, "_looks_like_runner", check)
    runners._extract(_tar(tmp_path, "one"), cancel=None, emit=lambda _e: None)
    assert seen == [True]
    assert (root / "SyntheticRunner" / "version").read_text() == "one"


def test_extract_lets_its_leases_go(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    runners._extract(_tar(tmp_path, "old"), cancel=None, emit=lambda _e: None)
    before = len(os.listdir("/proc/self/fd"))
    runners._extract(_tar(tmp_path, "new"), cancel=None, emit=lambda _e: None)
    assert len(os.listdir("/proc/self/fd")) == before


def test_without_flock_support_the_lease_falls_back_to_age(tmp_path, monkeypatch):
    backup = tmp_path / ".GE-X.old-nolock"
    backup.mkdir()
    (backup / ownerlease.MARKER).write_text(f"{DEAD_PID} lease\n")

    def no_locks(*_args):
        raise OSError(errno.ENOLCK, "no locks")

    monkeypatch.setattr(ownerlease.fcntl, "flock", no_locks)
    assert ownerlease.may_remove(backup, unknown_min_age=3600) is False
    _age(backup, 7200)
    _age(backup / ownerlease.MARKER, 7200)
    assert ownerlease.may_remove(backup, unknown_min_age=3600) is True


def test_a_held_lease_is_seen_as_held(tmp_path):
    folder = tmp_path / ".unpack-x"
    folder.mkdir()
    lease = ownerlease.take(folder)
    try:
        (folder / ownerlease.MARKER).write_text(f"{DEAD_PID} lease\n")  # PID says dead
        _age(folder, 10 * 86400)
        assert ownerlease.may_remove(folder, unknown_min_age=0) is False
    finally:
        lease.release()
    assert ownerlease.may_remove(folder, unknown_min_age=0) is True


def test_a_live_pid_in_this_namespace_still_keeps_it(tmp_path):
    backup = tmp_path / ".GE-X.old-mine"
    backup.mkdir()
    (backup / ownerlease.MARKER).write_text(str(os.getpid()))
    _age(backup, 10 * 86400)
    _age(backup / ownerlease.MARKER, 10 * 86400)
    assert ownerlease.may_remove(backup, unknown_min_age=0) is False
