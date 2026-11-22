# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive disappearance/replacement modelled by renaming disposable directories.

No real mount or user drive is changed. Inodes change just as they do when a
new directory tree becomes visible at a recorded library path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nadare.core import relocate


@pytest.mark.parametrize("state", ["missing", "replacement"])
def test_pending_cleanup_refuses_a_different_original_tree(tmp_path, state):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for folder in (old, new):
        (folder / "game.bin").write_bytes(b"game")
    profile = {"prefix": str(new)}
    profile.update(relocate.with_pending_original({}, profile, old, new))
    old.rename(tmp_path / "unplugged")
    if state == "replacement":
        old.mkdir()
        (old / "game.bin").write_bytes(b"game")
    assert relocate.pending_originals(SimpleNamespace(profiles={"g": profile})) == []
    assert (tmp_path / "unplugged" / "game.bin").read_bytes() == b"game"
    if state == "replacement":
        assert (old / "game.bin").read_bytes() == b"game"


def test_unplugged_move_source_is_recovered_after_reconnect(tmp_path):
    old, new = tmp_path / "source-disk" / "game", tmp_path / "dest-disk" / "game"
    old.mkdir(parents=True)
    (old / "save.dat").write_bytes(b"save")
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    old.parent.rename(tmp_path / "disconnected")
    relocate.finish_interrupted_moves({"g": profile})
    (tmp_path / "disconnected").rename(old.parent)
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(old)
    assert (old / "save.dat").read_bytes() == b"save"


def test_recovery_does_not_delete_a_replacement_disks_partial_folder(tmp_path):
    old, new = tmp_path / "old", tmp_path / "destination-disk" / "game"
    old.mkdir()
    (old / "game.bin").write_bytes(b"game")
    new.parent.mkdir()
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    new.parent.rename(tmp_path / "disconnected")
    new.parent.mkdir()
    partial = new.with_name(new.name + relocate.PARTIAL_SUFFIX)
    partial.mkdir()
    sentinel = partial / "belongs-to-replacement-disk.txt"
    sentinel.write_bytes(b"keep")
    relocate.finish_interrupted_moves({"g": profile})
    assert sentinel.exists(), "recovery deleted an unrecorded tree on a replacement disk"
    assert sentinel.read_bytes() == b"keep"


def test_recovery_does_not_adopt_unrelated_destination_when_source_disk_is_missing(tmp_path):
    old, new = tmp_path / "source-disk" / "game", tmp_path / "destination-disk" / "game"
    old.mkdir(parents=True)
    (old / "game.bin").write_bytes(b"real game")
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    old.parent.rename(tmp_path / "disconnected")
    new.mkdir(parents=True)
    (new / "unrelated.txt").write_bytes(b"other data")
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(old), "missing source was treated as proof the move completed"
    assert (tmp_path / "disconnected" / "game" / "game.bin").read_bytes() == b"real game"


def test_unknown_move_record_keeps_unrelated_partial_and_recovery_information(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    partial = new.with_name(new.name + relocate.PARTIAL_SUFFIX)
    partial.mkdir()
    (partial / "keep").write_bytes(b"keep")
    profile = {"prefix": str(old), relocate.MOVE_IN_PROGRESS: {"from": str(old), "to": str(new)}}
    relocate.finish_interrupted_moves({"g": profile})
    assert (partial / "keep").read_bytes() == b"keep"
    assert relocate.MOVE_IN_PROGRESS in profile


def test_failed_journal_save_never_starts_move(tmp_path, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui.settings_parts.move import _record_move

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "game").write_bytes(b"keep")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine", prefix=str(old))

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "save", fail)
    with pytest.raises(OSError):
        _record_move(cfg, "g", old, new)
    assert (old / "game").read_bytes() == b"keep" and not new.exists()


def test_valid_move_record_does_not_claim_a_preexisting_partial(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    partial = new.with_name(new.name + relocate.PARTIAL_SUFFIX)
    partial.mkdir()
    (partial / "keep").write_bytes(b"unowned")
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    relocate.finish_interrupted_moves({"g": profile})
    assert (partial / "keep").read_bytes() == b"unowned"


def test_copy_error_does_not_clean_up_a_replaced_partial(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "game").write_bytes(b"game")
    monkeypatch.setattr(relocate, "_same_device", lambda *a: False)
    partial = new.with_name(new.name + relocate.PARTIAL_SUFFIX)

    def replace_then_fail(done, total):
        partial.rename(tmp_path / "our-unfinished-copy")
        partial.mkdir()
        (partial / "keep").write_bytes(b"unowned")
        raise OSError("drive replaced during copy")

    with pytest.raises(relocate.RelocateError):
        relocate.move_tree(old, new, progress=replace_then_fail)
    assert (partial / "keep").read_bytes() == b"unowned"
    assert (old / "game").read_bytes() == b"game"
