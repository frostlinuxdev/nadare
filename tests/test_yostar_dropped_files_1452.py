# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 3d: an update removes the files the new Yostar manifest no longer lists.

Only files an earlier manifest recorded are ever removed: each finished sync records the
manifest's paths in the game folder, and the next finished sync removes what that record
lists and the new manifest does not. A player's own files, and every file of an install
that has no record yet (b25 and older), stay. A file that could not be removed is kept in
the leftover list and Repair tries again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_yostar_backend_directdl_1452 as base

from nadare.core import directdl
from nadare.core.backends import BackendError

# The local CDN and offline machinery of the 3b tests (fixtures, so assigned, not imported).
cdn = base.cdn
_offline_machinery = base._offline_machinery
GAME, _ak, _manifest = base.GAME, base._ak, base._manifest

OLD = {**GAME, "data/old.pak": b"O" * 512, "data/sub/gone.bin": b"G" * 32}
RECORD = Path(directdl.STATE_DIR) / "manifest-files.json"


def _install(cdn, tmp_path: Path, files: dict[str, bytes], version: str = "2.0.0"):
    cdn.files.update(files)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", files, cdn.base, version))
    backend.install()
    return backend


def _update(backend, cdn, tmp_path: Path, files: dict[str, bytes], version: str):
    cdn.files.update(files)
    backend.ctx.profile["manifest_file"] = str(
        _manifest(tmp_path, "Arknights_EN", files, cdn.base, version=version)
    )
    backend.update()


def test_a_finished_sync_records_the_manifest_paths(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    record = json.loads((backend.install_dir() / RECORD).read_text(encoding="utf-8"))
    assert record["game"] == "Arknights_EN"
    assert record["version"] == "2.0.0"
    assert record["paths"] == sorted(OLD)


def test_an_update_removes_files_the_new_manifest_dropped(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    mine = root / "data" / "my-notes.txt"
    mine.write_text("player's own file")
    _update(backend, cdn, tmp_path, GAME, "2.1.0")
    assert not (root / "data/old.pak").exists()
    assert not (root / "data/sub/gone.bin").exists()
    for path, data in GAME.items():
        assert (root / path).read_bytes() == data
    assert mine.read_text() == "player's own file"  # never listed: never removed
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    assert record["version"] == "2.1.0" and record["paths"] == sorted(GAME)
    assert "leftover_files" not in backend.status().data


def test_an_install_without_a_record_removes_nothing_then_starts_recording(cdn, tmp_path):
    """A b25 install has no record: what it holds is unknown, so nothing goes."""
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    (root / RECORD).unlink()
    kept = {**GAME, "data/sub/gone.bin": OLD["data/sub/gone.bin"]}
    _update(backend, cdn, tmp_path, kept, "2.1.0")
    assert (root / "data/old.pak").exists()
    assert (root / RECORD).exists()
    # The next update knows what 2.1.0 had.
    _update(backend, cdn, tmp_path, GAME, "2.2.0")
    assert not (root / "data/sub/gone.bin").exists()
    assert (root / "data/old.pak").exists()


def test_a_failed_update_removes_nothing_and_keeps_the_record(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    before = (root / RECORD).read_bytes()
    new = {**GAME, "data/new.pak": b"N" * 64}
    cdn.missing.add("data/new.pak")
    with pytest.raises(BackendError):
        _update(backend, cdn, tmp_path, new, "2.1.0")
    assert (root / "data/old.pak").exists()
    assert (root / RECORD).read_bytes() == before


def test_a_tampered_record_never_reaches_outside_the_game_folder(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "victim.bin").write_text("keep")
    (root / "linked").symlink_to(elsewhere)
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    record["paths"] += ["../../../../outside.txt", str(outside), "linked/victim.bin"]
    (root / RECORD).write_text(json.dumps(record), encoding="utf-8")
    _update(backend, cdn, tmp_path, GAME, "2.1.0")  # the update itself succeeds
    assert outside.read_text() == "keep"
    assert (elsewhere / "victim.bin").read_text() == "keep"
    assert not (root / "data/old.pak").exists()


def test_another_games_record_is_ignored(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    record["game"] = "StellaSora_EN"
    (root / RECORD).write_text(json.dumps(record), encoding="utf-8")
    _update(backend, cdn, tmp_path, GAME, "2.1.0")
    assert (root / "data/old.pak").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_file_that_could_not_be_removed_is_retried_by_repair(cdn, tmp_path):
    backend = _install(cdn, tmp_path, OLD)
    root = backend.install_dir()
    stuck = root / "data/sub"
    stuck.chmod(0o500)
    try:
        _update(backend, cdn, tmp_path, GAME, "2.1.0")
        assert (stuck / "gone.bin").exists()
        assert backend.status().data.get("leftover_files") == 1
    finally:
        stuck.chmod(0o700)
    backend.repair()
    assert not (stuck / "gone.bin").exists()
    assert "leftover_files" not in backend.status().data
