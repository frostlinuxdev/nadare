# SPDX-License-Identifier: GPL-3.0-or-later
"""#1481 / #1483: every user-triggered recursive delete crosses no mount, and an unknown mount
list means "delete nothing", never "there are no mounts".

The unit tests stand a mount in with the kernel's mount list (``_mountinfo_points``), which is
how the helper sees a same-disk bind mount. The namespace test makes a real bind mount where the
host allows unprivileged user namespaces.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import paths, profilefolders, removal, runners


def _tree(root: Path) -> Path:
    (root / "own").mkdir(parents=True)
    (root / "own" / "a.bin").write_bytes(b"x")
    mnt = root / "mounted"
    mnt.mkdir()
    (mnt / "save.dat").write_bytes(b"precious")
    return mnt


def _pretend_mounted(monkeypatch, *folders: Path) -> None:
    points = frozenset(os.path.realpath(f) for f in folders)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: points)


# ------------------------------------------------------------- the shared helper
def test_an_unreadable_mount_list_deletes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "victim"
    mnt = _tree(root)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: None)
    outcome = removal.delete_tree(root)
    assert (mnt / "save.dat").read_bytes() == b"precious"
    assert (root / "own" / "a.bin").exists()  # refused before any traversal
    assert outcome.leftovers and outcome.leftovers[0][0] == root
    assert "mount" in str(outcome.leftovers[0][1])


def test_a_folder_that_cannot_be_inspected_is_kept_as_a_possible_mount(tmp_path, monkeypatch):
    mounts = removal._Mounts(0, frozenset())
    real_stat = Path.stat

    def stat(self, *a, **k):
        if self.name == "mounted":
            raise PermissionError("no")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    assert removal._is_mount(tmp_path / "mounted", mounts) is True


def test_the_normal_case_still_deletes(tmp_path, monkeypatch):
    root = tmp_path / "victim"
    _tree(root)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: frozenset())
    outcome = removal.delete_tree(root)
    assert not root.exists() and outcome.mounts == [] and outcome.leftovers == []


# ------------------------------------------------------------- orphan profile folders
def _orphan(tmp_path) -> tuple[Path, Path]:
    home = paths.data_dir() / "profiles"
    folder = home / "gone-game"
    mnt = _tree(folder)
    return folder, mnt


def test_orphan_folder_cleanup_keeps_a_mounted_folder(tmp_path, monkeypatch):
    folder, mnt = _orphan(tmp_path)
    assert [f.path for f in profilefolders.find({}, [])] == [folder]
    _pretend_mounted(monkeypatch, mnt)
    assert profilefolders.remove(folder, {}, []) is False  # not fully deleted: say so
    assert (mnt / "save.dat").read_bytes() == b"precious"
    assert not (folder / "own").exists()  # the folder's own files still go


def test_orphan_folder_cleanup_refuses_without_a_mount_list(tmp_path, monkeypatch):
    folder, mnt = _orphan(tmp_path)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: None)
    assert profilefolders.remove(folder, {}, []) is False
    assert (mnt / "save.dat").exists() and (folder / "own" / "a.bin").exists()


def test_orphan_folder_cleanup_still_deletes_a_plain_folder(tmp_path, monkeypatch):
    folder, _mnt = _orphan(tmp_path)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: frozenset())
    assert profilefolders.remove(folder, {}, []) is True
    assert not folder.exists()


# ------------------------------------------------------------------------ runners
def test_runner_removal_keeps_a_mounted_folder(tmp_path, monkeypatch):
    runner = paths.runners_dir() / "GE-Proton-test"
    mnt = _tree(runner)
    _pretend_mounted(monkeypatch, mnt)
    assert runners.remove(runner) is False
    assert (mnt / "save.dat").read_bytes() == b"precious"


def test_runner_removal_still_deletes_a_plain_runner(tmp_path, monkeypatch):
    runner = paths.runners_dir() / "GE-Proton-test"
    _tree(runner)
    monkeypatch.setattr(removal, "_mountinfo_points", lambda: frozenset())
    assert runners.remove(runner) is True
    assert not runner.exists()


# ------------------------------------------------------------- a real bind mount
_NS = r"""
import os, subprocess, sys
from pathlib import Path
base = Path(sys.argv[1]); outside = base / "outside"; outside.mkdir()
(outside / "save.dat").write_bytes(b"precious")
os.environ["XDG_DATA_HOME"] = str(base / "data")
from nadare.core import paths, profilefolders, runners
which = sys.argv[2]
orphan = paths.data_dir() / "profiles" / "gone"
folder = orphan if which == "orphan" else paths.runners_dir() / "GE-x"
(folder / "mnt").mkdir(parents=True); (folder / "own").write_bytes(b"x")
subprocess.run(["mount", "--bind", str(outside), str(folder / "mnt")], check=True)
ok = profilefolders.remove(folder, {}, []) if which == "orphan" else runners.remove(folder)
subprocess.run(["umount", str(folder / "mnt")], check=False)
print("KEPT" if (outside / "save.dat").exists() else "LOST", ok)
"""


@pytest.mark.parametrize("which", ["orphan", "runner"])
def test_a_real_bind_mount_survives(tmp_path, which):
    if not shutil.which("unshare") or not shutil.which("mount"):
        pytest.skip("unshare/mount not available")
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "true"], capture_output=True
    )
    if probe.returncode != 0:
        pytest.skip("unprivileged user+mount namespaces are not allowed here")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            sys.executable,
            "-c",
            _NS,
            str(tmp_path),
            which,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.split()[0] == "KEPT", out.stdout


def test_an_unreadable_mountinfo_is_unknown_not_empty(monkeypatch):
    import builtins

    real_open = builtins.open

    def fake_open(file, *a, **k):
        if str(file) == "/proc/self/mountinfo":
            raise PermissionError("denied")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert removal._mountinfo_points() is None
