# SPDX-License-Identifier: GPL-3.0-or-later
"""An in-app update killed at any step leaves the launch path usable (#1471).

The swap used to rename the old AppImage aside first and move the new one in second: a kill in
between left the path desktop and Steam shortcuts start missing. Here a child process is killed
right before each file operation the swap makes, in turn, and the old path must still start the
old or the new version every time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import selfupdate

SRC = Path(selfupdate.__file__).resolve().parents[2]
OLD, NEW = b"synthetic old image", b"synthetic new image"

_CHILD = r"""
import os, shutil, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nadare.core import selfupdate
kill_at = int(sys.argv[5])
count = 0
def wrap(real):
    def op(*a, **k):
        global count
        count += 1
        if count == kill_at:
            os._exit(73)
        return real(*a, **k)
    return op
for mod in (selfupdate.os,):
    for name in ("replace", "rename", "link", "symlink", "unlink"):
        setattr(mod, name, wrap(getattr(mod, name)))
shutil.copy2 = wrap(shutil.copy2)
shutil.copyfile = wrap(shutil.copyfile)
selfupdate.install_update(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
print("DONE", count)
"""


def _run(tmp: Path, filename: str, kill_at: int) -> subprocess.CompletedProcess:
    folder = tmp / f"k{kill_at}"
    folder.mkdir()
    old = folder / "Nadare-0.1.0b24-x86_64.AppImage"
    old.write_bytes(OLD)
    incoming = folder / ".incoming"
    incoming.write_bytes(NEW)
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(SRC), str(incoming), str(old), filename, str(kill_at)],
        capture_output=True, text=True, timeout=30,
    ), old


@pytest.mark.parametrize(
    "filename", ["Nadare-0.1.0b25-x86_64.AppImage", "Nadare-0.1.0b24-x86_64.AppImage"]
)
def test_a_kill_before_any_file_operation_keeps_the_launch_path_usable(tmp_path, filename):
    finished = False
    for kill_at in range(1, 40):
        result, old = _run(tmp_path, filename, kill_at)
        assert result.returncode in (0, 73), result.stderr
        assert old.exists(), f"killed before operation {kill_at}: {old.name} is gone"
        assert old.read_bytes() in (OLD, NEW), f"killed before operation {kill_at}: wrong bytes"
        if result.returncode == 0:
            finished = True
            break
    assert finished, "the swap never completed"
    dest = old.with_name(filename)
    assert dest.read_bytes() == NEW
    assert old.read_bytes() == NEW  # the old path leads to the new version
    backup = old.with_name(old.name + selfupdate.BACKUP_SUFFIX)
    assert backup.read_bytes() == OLD


def test_the_old_version_is_kept_when_hard_links_are_refused(tmp_path, monkeypatch):
    old = tmp_path / "Nadare-0.1.0b24-x86_64.AppImage"
    old.write_bytes(OLD)
    incoming = tmp_path / ".incoming"
    incoming.write_bytes(NEW)

    def no_links(*_a, **_k):
        raise PermissionError(1, "hard links not allowed here")

    monkeypatch.setattr(selfupdate.os, "link", no_links)
    dest = selfupdate.install_update(incoming, old, "Nadare-0.1.0b25-x86_64.AppImage")
    assert dest.read_bytes() == NEW and old.read_bytes() == NEW
    assert old.with_name(old.name + selfupdate.BACKUP_SUFFIX).read_bytes() == OLD


def test_an_older_backup_is_replaced_by_the_current_version(tmp_path):
    old = tmp_path / "Nadare-0.1.0b24-x86_64.AppImage"
    old.write_bytes(OLD)
    backup = old.with_name(old.name + selfupdate.BACKUP_SUFFIX)
    backup.write_bytes(b"b23")
    incoming = tmp_path / ".incoming"
    incoming.write_bytes(NEW)
    selfupdate.install_update(incoming, old, old.name)
    assert backup.read_bytes() == OLD and old.read_bytes() == NEW
