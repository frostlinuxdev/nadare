# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.journal."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core.journal import JOURNAL_NAME, JournalError, Transaction, safe_relative


def test_safe_relative() -> None:
    assert safe_relative("a/b.txt") == "a/b.txt"
    assert safe_relative("a\\b.txt") == "a/b.txt"
    assert safe_relative("./a/../b") == "b"

    for bad in ("", ".", "..", "../escape", "/abs", JOURNAL_NAME + "/x"):
        with pytest.raises(JournalError):
            safe_relative(bad)


def test_write_commit_and_load(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    tx = Transaction(root).begin(version=3)
    tx.write("data/file.bin", b"new contents")
    assert (root / "data" / "file.bin").read_bytes() == b"new contents"
    tx.commit()

    state = Transaction.load(root)
    assert state["phase"] == "committed"
    assert state["version"] == 3
    assert state["operations"][0]["path"] == "data/file.bin"


def test_begin_twice_raises(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    tx = Transaction(root).begin()
    with pytest.raises(JournalError):
        tx.begin()
    assert (root / JOURNAL_NAME / "journal.json").is_file()


def test_rollback_restores_previous_file(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    target = root / "save.dat"
    target.write_bytes(b"old")

    tx = Transaction(root).begin()
    tx.write("save.dat", b"new")
    assert target.read_bytes() == b"new"

    tx.rollback()
    assert target.read_bytes() == b"old"
    assert tx.state["phase"] == "rolled_back"


def test_crash_recovery_rolls_back_interrupted_apply(tmp_path: Path) -> None:
    """Simulate a crash after ``applying`` began: recover() must undo it."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "existing.bin").write_bytes(b"original")

    tx = Transaction(root).begin()
    tx.write("existing.bin", b"modified")
    tx.write("brand-new.bin", b"created")
    # ... crash: no commit(), no rollback().

    recovered = Transaction.recover(root)

    assert recovered is not None
    assert recovered["phase"] == "rolled_back"
    assert (root / "existing.bin").read_bytes() == b"original"
    assert not (root / "brand-new.bin").exists()


def test_recover_returns_none_without_journal(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    assert Transaction.recover(root) is None


def test_recover_leaves_verified_transaction_for_finalize(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    tx = Transaction(root).begin()
    tx.write("a.bin", b"aa")
    tx.verify()

    recovered = Transaction.recover(root)
    assert recovered is not None
    assert recovered["phase"] == "files_verified"

    history = tx.finalize()
    assert history.is_dir()
    assert not (root / JOURNAL_NAME).exists()


def test_finalize_refuses_applying_transaction(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    tx = Transaction(root).begin()
    tx.write("a.bin", b"aa")
    with pytest.raises(JournalError):
        tx.finalize()


def test_truncated_journal_is_detected(tmp_path: Path) -> None:
    """A corrupted/truncated journal must fail loudly, not silently corrupt."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "keep.bin").write_bytes(b"keep")

    tx = Transaction(root).begin()
    tx.write("keep.bin", b"changed")

    journal_file = root / JOURNAL_NAME / "journal.json"
    raw = journal_file.read_text(encoding="utf-8")
    journal_file.write_text(raw[: len(raw) // 2], encoding="utf-8")

    with pytest.raises(JournalError):
        Transaction.recover(root)


def test_load_rejects_invalid_phase(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    journal = root / JOURNAL_NAME
    journal.mkdir()
    (journal / "journal.json").write_text(
        json.dumps({"phase": "bogus", "operations": []}), encoding="utf-8"
    )
    with pytest.raises(JournalError):
        Transaction.load(root)


def test_load_rejects_duplicate_paths(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    journal = root / JOURNAL_NAME
    journal.mkdir()
    op = {"path": "a", "backup": None, "backup_sha256": None, "directory": True, "temporary": None}
    (journal / "journal.json").write_text(
        json.dumps({"phase": "applying", "operations": [op, dict(op)]}), encoding="utf-8"
    )
    with pytest.raises(JournalError):
        Transaction.load(root)


def test_begin_refuses_existing_journal(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    Transaction(root).begin()
    with pytest.raises(JournalError):
        Transaction(root).begin()


def test_begin_refuses_preexisting_journal_dir(tmp_path: Path) -> None:
    """L2: an existing journal path must surface as JournalError, not
    FileExistsError, and must not be created concurrently."""
    root = tmp_path / "game"
    root.mkdir()
    (root / JOURNAL_NAME).mkdir()
    with pytest.raises(JournalError):
        Transaction(root).begin()


def test_atomic_json_fsyncs_the_directory(tmp_path: Path, monkeypatch) -> None:
    """M7: the rename itself must be durable, not just the file contents."""
    from nadare.core import journal

    synced: list[Path] = []
    monkeypatch.setattr(journal, "_sync_dir", lambda p: synced.append(Path(p)))
    path = tmp_path / "journal.json"
    journal._atomic_json(path, {"phase": "applying", "operations": []})
    assert path.is_file()
    assert synced == [tmp_path]


def test_finalize_prunes_old_histories(tmp_path: Path) -> None:
    """L3: archived journals are bounded so a long-lived install cannot leak."""
    from nadare.core.journal import KEEP_HISTORY

    root = tmp_path / "game"
    root.mkdir()
    for index in range(KEEP_HISTORY + 4):
        old = root / f".nadare-update-history-old{index}"
        old.mkdir()
        os.utime(old, (1000 + index, 1000 + index))

    tx = Transaction(root).begin()
    tx.write("a.bin", b"aa")
    tx.verify()
    tx.finalize()

    remaining = list(root.glob(".nadare-update-history-*"))
    assert len(remaining) == KEEP_HISTORY


def test_load_rejects_temporary_escaping_root(tmp_path: Path) -> None:
    """#83: a crafted ``temporary`` must not be allowed to escape the root."""
    root = tmp_path / "game"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")

    (root / "save.dat").write_bytes(b"old")
    tx = Transaction(root).begin()
    tx.write("save.dat", b"new")
    # ... crash: no commit(), no rollback().

    journal_file = root / JOURNAL_NAME / "journal.json"
    state = json.loads(journal_file.read_text(encoding="utf-8"))
    state["operations"][0]["temporary"] = "../../victim"
    journal_file.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(JournalError):
        Transaction.load(root)
    with pytest.raises(JournalError):
        Transaction.recover(root)

    assert victim.read_bytes() == b"untouched"


def test_load_rejects_corrupted_backup(tmp_path: Path) -> None:
    """#86: a tampered backup file must be caught by load() itself."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "save.dat").write_bytes(b"old")

    tx = Transaction(root).begin()
    tx.write("save.dat", b"new")

    backup = root / JOURNAL_NAME / "0"
    assert backup.is_file()
    backup.write_bytes(b"tampered")

    with pytest.raises(JournalError):
        Transaction.load(root)
