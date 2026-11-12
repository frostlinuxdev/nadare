# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: Kuro update patches as a speed-up in front of the whole-file sync."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from _krpdiff import build

from nadare.core import directdl, kuropatch

ROOT = "https://cdn.example/"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


OLD_A = bytes(range(256)) * 64
OLD_B = b"b-old" * 500
NEW_A = OLD_A[:4000] + b"CHANGED" + OLD_A[4000:]
NEW_B = OLD_B[100:] + b"more"


def _patch_bytes() -> bytes:
    return build(
        [("Client/a.pak", OLD_A), ("Client/b.pak", OLD_B)],
        [("Client/a.pak", NEW_A), ("Client/b.pak", NEW_B)],
        [("old", 0, 4000), ("new", b"CHANGED"), ("old", 4000, len(OLD_A) - 4000),
         ("old", len(OLD_A) + 100, len(OLD_B) - 100), ("new", b"more")],
    )


@pytest.fixture()
def game(tmp_path, monkeypatch):
    root = tmp_path / "install"
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "a.pak").write_bytes(OLD_A)
    (root / "Client" / "b.pak").write_bytes(OLD_B)
    (root / "Client" / "gone.pak").write_bytes(b"retired")
    patch = _patch_bytes()
    listing = {
        "resource": [{"dest": "g0.krpdiff", "md5": md5(patch), "size": len(patch)}],
        "groupInfos": [{
            "dest": "g0.krpdiff",
            "srcFiles": [
                {"dest": "Client/a.pak", "md5": md5(OLD_A), "size": len(OLD_A)},
                {"dest": "Client/b.pak", "md5": md5(OLD_B), "size": len(OLD_B)},
            ],
            "dstFiles": [
                {"dest": "Client/a.pak", "md5": md5(NEW_A), "size": len(NEW_A)},
                {"dest": "Client/b.pak", "md5": md5(NEW_B), "size": len(NEW_B)},
            ],
        }],
        "deleteFiles": ["Client/gone.pak", "Client/a.pak"],
    }
    served = {f"{ROOT}patch/3.7.2/index.json": json.dumps(listing).encode()}
    files = {f"{ROOT}patch/3.7.2/resources/g0.krpdiff": patch}
    fetched: list[str] = []

    def fetch(url, headers=None):
        fetched.append(url)
        return served[url]

    def download(rf, path, *, emit=None, cancel=None, pool=None):
        data = files[rf.url]
        assert md5(data) == rf.md5
        Path(path).write_bytes(data)

    monkeypatch.setattr(directdl, "_download_remote", download)
    plan = directdl.Plan(
        game_tag="wuwa", version="3.8.0",
        files=(
            directdl.RemoteFile("Client/a.pak", "https://x/a", len(NEW_A), md5(NEW_A)),
            directdl.RemoteFile("Client/b.pak", "https://x/b", len(NEW_B), md5(NEW_B)),
        ),
        extra={kuropatch.EXTRA_KEY: {"root": ROOT, "packs": {"common": [
            {"version": "3.7.1", "indexFile": "nope", "baseUrl": "nope"},
            {"version": "3.7.2", "indexFile": "patch/3.7.2/index.json",
             "baseUrl": "patch/3.7.2/resources"},
        ]}}},
    )
    return root, plan, fetch, fetched, listing


def test_installed_versions_reads_a_moved_pack():
    both = kuropatch.installed_versions("3.7.2", ["common", "hd"])
    assert both == {"common": "3.7.2", "hd": "3.7.2"}
    assert kuropatch.installed_versions("3.7.2+hd-3.7.3", ["common", "hd"]) == {
        "common": "3.7.2", "hd": "3.7.3"}


def test_prepare_picks_the_installed_versions_patch(game):
    root, plan, fetch, fetched, _ = game
    patches = kuropatch.prepare(plan, "3.7.2", fetch)
    assert fetched == [f"{ROOT}patch/3.7.2/index.json"]
    assert len(patches.groups) == 1
    assert patches.groups[0].patch.url == f"{ROOT}patch/3.7.2/resources/g0.krpdiff"
    # A file the new version still lists is never retired.
    assert patches.deletes == ("Client/gone.pak",)


def test_no_patch_for_another_version(game):
    _, plan, fetch, fetched, _ = game
    assert kuropatch.prepare(plan, "3.6.0", fetch).groups == ()
    assert fetched == []


def test_a_path_leaving_the_install_is_refused(game):
    _, plan, fetch, _, listing = game
    listing["deleteFiles"] = ["../../etc/passwd"]
    with pytest.raises(directdl.DirectError):
        kuropatch.prepare(plan, "3.7.2", lambda url, h=None: json.dumps(listing).encode())


def test_apply_patches_the_files_in_place(game):
    root, plan, fetch, _, _ = game
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.applied == 1 and outcome.skipped == 0
    assert (root / "Client" / "a.pak").read_bytes() == NEW_A
    assert (root / "Client" / "b.pak").read_bytes() == NEW_B
    assert not (root / directdl.STATE_DIR / kuropatch.STAGING).exists()


def test_a_changed_old_file_skips_the_group_untouched(game):
    root, plan, fetch, _, _ = game
    (root / "Client" / "b.pak").write_bytes(OLD_B[:-1] + b"X")
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.applied == 0 and outcome.skipped == 1
    assert (root / "Client" / "a.pak").read_bytes() == OLD_A


def test_a_patch_output_the_game_index_disagrees_with_is_not_used(game):
    root, plan, fetch, _, _ = game
    patches = kuropatch.prepare(plan, "3.7.2", fetch)
    other = directdl.Plan(
        game_tag="wuwa", version="3.8.0",
        files=(directdl.RemoteFile("Client/a.pak", "https://x/a", 1, "0" * 32), plan.files[1]),
    )
    outcome = kuropatch.apply(patches, other, root)
    assert outcome.applied == 0
    assert (root / "Client" / "a.pak").read_bytes() == OLD_A


def test_a_damaged_patch_download_changes_nothing(game, monkeypatch):
    root, plan, fetch, _, _ = game

    def broken(rf, path, **kwargs):
        Path(path).write_bytes(b"HDIFF19&zstd&fadler64\0garbage")

    monkeypatch.setattr(directdl, "_download_remote", broken)
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.skipped == 1
    assert (root / "Client" / "a.pak").read_bytes() == OLD_A
    assert (root / "Client" / "b.pak").read_bytes() == OLD_B


def test_not_enough_space_skips_the_group(game, monkeypatch):
    root, plan, fetch, _, _ = game
    monkeypatch.setattr(kuropatch.diskspace, "free_bytes", lambda path: 10)
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.skipped == 1 and "space" in outcome.notes[0]


def test_already_patched_files_are_not_downloaded_again(game, monkeypatch):
    root, plan, fetch, _, _ = game
    (root / "Client" / "a.pak").write_bytes(NEW_A)
    (root / "Client" / "b.pak").write_bytes(NEW_B)
    monkeypatch.setattr(directdl, "_download_remote", lambda *a, **k: pytest.fail("downloaded"))
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.applied == 0 and "already" in outcome.notes[0]


def test_delete_retired_removes_only_plain_files_inside(tmp_path):
    root = tmp_path / "install"
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "old.pak").write_bytes(b"x")
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "f.pak").write_bytes(b"precious")
    os.symlink(outside, root / "Linked")
    os.symlink(outside / "f.pak", root / "Client" / "link.pak")
    removed = kuropatch.delete_retired(
        root, ("Client/old.pak", "Linked/f.pak", "Client/link.pak", "Client/missing.pak")
    )
    assert removed == 1
    assert not (root / "Client" / "old.pak").exists()
    assert (outside / "f.pak").read_bytes() == b"precious"


def test_a_source_mismatch_downloads_nothing(game, monkeypatch):
    root, plan, fetch, _, _ = game
    (root / "Client" / "b.pak").write_bytes(OLD_B[:-1] + b"X")
    monkeypatch.setattr(directdl, "_download_remote", lambda *a, **k: pytest.fail("downloaded"))
    outcome = kuropatch.apply(kuropatch.prepare(plan, "3.7.2", fetch), plan, root)
    assert outcome.skipped == 1


def test_a_patch_that_builds_other_bytes_than_promised_replaces_nothing(game, monkeypatch):
    root, plan, fetch, _, listing = game
    promised = b"what the index says"
    listing["groupInfos"][0]["dstFiles"][0].update(md5=md5(promised), size=len(promised))
    plan = directdl.Plan(
        game_tag="wuwa", version="3.8.0",
        files=(directdl.RemoteFile("Client/a.pak", "https://x/a", len(promised), md5(promised)),
               plan.files[1]),
        extra=plan.extra,
    )
    served = json.dumps(listing).encode()
    outcome = kuropatch.apply(
        kuropatch.prepare(plan, "3.7.2", lambda url, h=None: served), plan, root
    )
    assert outcome.applied == 0 and "did not come out" in outcome.notes[0]
    assert (root / "Client" / "a.pak").read_bytes() == OLD_A
    assert (root / "Client" / "b.pak").read_bytes() == OLD_B


def test_delete_retired_leaves_anything_that_is_not_a_plain_file(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    os.mkfifo(root / "pipe.pak")
    (root / "dir.pak").mkdir()
    assert kuropatch.delete_retired(root, ("pipe.pak", "dir.pak")) == 0
    assert (root / "pipe.pak").exists() and (root / "dir.pak").is_dir()
