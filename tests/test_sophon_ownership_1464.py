# SPDX-License-Identifier: GPL-3.0-or-later
"""#1464: Sophon answers "which files are mine?"; the sync coordinator no longer reads its receipt.

``directdl.sync`` used to build the receipt path, read the raw text and call Sophon's private
validator before pruning stale partial downloads. Now ``sophon.owned_files`` is the one reader:
it returns the validated paths, or says it is not sure, and every unsure answer keeps the parts.
"""

from __future__ import annotations

import inspect
import os

import pytest
from test_sophon import Build, plan_for, servers  # noqa: F401 - fixture reuse

from nadare.core import directdl as dd
from nadare.core import sophon


def _pkg():
    return sophon.SophonPackage(
        name="game", manifest_url="http://127.0.0.1:9/m", chunk_url="http://127.0.0.1:9/c",
        manifest_id="m1", size=100,
    )


def _receipt(root, pkg, data: bytes):
    path = root / sophon.STATE_DIR / f"sophon-{pkg.name}.files"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_valid_receipt_lists_its_files(tmp_path):
    pkg = _pkg()
    _receipt(tmp_path, pkg, b"a.bin\nsub/b.pak\n\n")
    owned = sophon.owned_files(pkg, tmp_path)
    assert owned.certain
    assert owned.paths == frozenset({"a.bin", "sub/b.pak"})


def test_an_empty_receipt_is_certain_and_owns_nothing(tmp_path):
    pkg = _pkg()
    _receipt(tmp_path, pkg, b"")
    owned = sophon.owned_files(pkg, tmp_path)
    assert owned.certain and owned.paths == frozenset()


@pytest.mark.parametrize(
    "data",
    [
        b"a.bin\n../outside.bin\n",  # hostile: climbs out of the game folder
        b"a.bin\n/etc/passwd\n",  # hostile: absolute
        b"a.bin\n.nadare/sophon-x.done\n",  # Nadare's own state is never a member
        b"a.bin\nbad\x00name\n",  # NUL
        b"a.bin\n\xff\xfe\n",  # not UTF-8
    ],
)
def test_a_damaged_or_hostile_receipt_is_not_certain(tmp_path, data):
    pkg = _pkg()
    _receipt(tmp_path, pkg, data)
    owned = sophon.owned_files(pkg, tmp_path)
    assert not owned.certain
    assert owned.paths == frozenset()
    assert owned.reason


def test_a_missing_receipt_is_not_certain(tmp_path):
    owned = sophon.owned_files(_pkg(), tmp_path)
    assert not owned.certain and owned.reason


def test_a_receipt_that_is_a_link_is_not_certain(tmp_path):
    pkg = _pkg()
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("a.bin\n")
    path = tmp_path / sophon.STATE_DIR / f"sophon-{pkg.name}.files"
    path.parent.mkdir(parents=True)
    path.symlink_to(elsewhere)
    assert not sophon.owned_files(pkg, tmp_path).certain


def test_a_receipt_that_is_a_folder_is_not_certain(tmp_path):
    pkg = _pkg()
    (tmp_path / sophon.STATE_DIR / f"sophon-{pkg.name}.files").mkdir(parents=True)
    assert not sophon.owned_files(pkg, tmp_path).certain


def test_the_coordinator_does_not_read_sophon_receipts():
    source = inspect.getsource(dd)
    assert "sophon._" not in source


def _stale_part(root):
    stale = root / dd.DL_DIR / "abandoned.bin.part"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old partial download")
    return stale


def test_a_valid_receipt_still_lets_stale_parts_go(servers, tmp_path):  # noqa: F811
    build = Build({"keep.bin": b"k" * 100}, chunk_size=100)
    servers(build)
    stale = _stale_part(tmp_path)
    dd.sync(plan_for(build), tmp_path, workers=1)
    assert not stale.exists()
    assert (tmp_path / "keep.bin").read_bytes() == b"k" * 100


def test_an_unsure_receipt_keeps_every_part(servers, tmp_path, monkeypatch):  # noqa: F811
    build = Build({"keep.bin": b"k" * 100}, chunk_size=100)
    servers(build)
    stale = _stale_part(tmp_path)
    monkeypatch.setattr(
        sophon, "owned_files",
        lambda pkg, root: sophon.Ownership(frozenset(), False, "test: unsure"),
        raising=False,
    )
    dd.sync(plan_for(build), tmp_path, workers=1)
    assert stale.read_bytes() == b"old partial download"


def test_a_member_named_like_a_part_is_kept(servers, tmp_path):  # noqa: F811
    """A game file that really ends in .part is listed in the receipt and never pruned."""
    build = Build({"keep.bin": b"k" * 100, "data.nadare-new.part": b"p" * 100}, chunk_size=100)
    servers(build)
    dd.sync(plan_for(build), tmp_path, workers=1)
    dd.sync(plan_for(build), tmp_path, workers=1)
    assert (tmp_path / "data.nadare-new.part").read_bytes() == b"p" * 100


def test_an_interrupted_receipt_write_leaves_the_old_one_whole(tmp_path, monkeypatch):
    """Sophon writes the receipt through a temp file; a crash mid-write leaves the old one,
    which still answers (the temp file is never read as the receipt)."""
    pkg = _pkg()
    _receipt(tmp_path, pkg, b"a.bin\n")
    temp = tmp_path / sophon.STATE_DIR / f".sophon-{pkg.name}.files.x.tmp"
    temp.write_bytes(b"a.bin\nhalf-writ")
    owned = sophon.owned_files(pkg, tmp_path)
    assert owned.certain and owned.paths == frozenset({"a.bin"})
    assert os.path.exists(temp)
