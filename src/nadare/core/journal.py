# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Crash-safe update journal.

A transaction is a directory (default ``<root>/.nadare-update-transaction``)
holding ``journal.json`` plus one backup file per pre-existing destination,
named by operation index. The phase machine is::

    applying -> files_verified -> committed
    applying -> rolled_back

Every mutation backs up the old file *before* touching it and appends to the
journal with an atomic write. If the process dies mid-update, :meth:`recover`
re-reads the journal and rolls back (phase ``applying``) or finalises
(``files_verified``/``committed``). Relative paths are validated so a journal
can never be tricked into writing outside the game root.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

JOURNAL_NAME = ".nadare-update-transaction"
PHASES = ("applying", "files_verified", "committed", "rolled_back")
MAX_OPERATIONS = 100_000

#: How many archived journals to keep; older ones are pruned when finalising.
KEEP_HISTORY = 5


class JournalError(RuntimeError):
    """The journal is corrupt or the requested action is invalid in this phase."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_relative(path: str) -> str:
    """Validate and normalise a journal-relative path (POSIX form)."""
    text = str(path).replace("\\", "/").strip()
    if not text or text.startswith("/") or text.startswith(JOURNAL_NAME):
        raise JournalError(f"unsafe journal path: {path!r}")
    norm = posixpath.normpath(text)
    if norm in (".", "..") or norm.startswith("../") or norm.startswith(JOURNAL_NAME):
        raise JournalError(f"unsafe journal path: {path!r}")
    return norm


class Transaction:
    """A durable, reversible set of file operations under ``root``."""

    def __init__(
        self, root: str | os.PathLike[str], journal_dir: str | os.PathLike[str] | None = None
    ) -> None:
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise JournalError(f"journal root is not a directory: {self.root}")
        self.journal = Path(journal_dir) if journal_dir else self.root / JOURNAL_NAME
        self.state: dict[str, Any] = {"phase": "applying", "operations": []}
        self._begun = False

    # ---- persistence -------------------------------------------------------
    @property
    def journal_file(self) -> Path:
        return self.journal / "journal.json"

    def save(self) -> None:
        _atomic_json(self.journal_file, self.state)

    def begin(self, *, version: int | None = None) -> Transaction:
        if self._begun:
            raise JournalError("transaction already begun")
        try:
            # ``exist_ok=False`` closes the check-then-create race: two callers
            # cannot both believe they created the journal, and the loser gets a
            # ``JournalError`` instead of a bare ``FileExistsError``.
            self.journal.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise JournalError(f"a journal already exists at {self.journal}") from exc
        _sync_dir(self.journal.parent)
        self.state = {"phase": "applying", "operations": []}
        if version is not None:
            self.state["version"] = int(version)
        self.save()
        self._begun = True
        return self

    # ---- path helpers ------------------------------------------------------
    def _target(self, rel: str) -> Path:
        rel = safe_relative(rel)
        target = (self.root / rel).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise JournalError(f"journal path escapes root: {rel!r}")
        return target

    @staticmethod
    def _temporary(index: int) -> str:
        return f".nadare-new-{index}"

    # ---- recording ---------------------------------------------------------
    def record(self, rel: str, *, directory: bool = False) -> dict[str, Any]:
        """Back up an existing destination and append the operation."""
        rel = safe_relative(rel)
        if not self._begun:
            raise JournalError("call begin() first")
        target = self._target(rel)
        operations = self.state["operations"]
        index = len(operations)

        backup: str | None = None
        backup_sha256: str | None = None
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise JournalError(f"destination is not a regular file: {rel}")
            backup = str(index)
            shutil.copy2(target, self.journal / backup)
            with open(self.journal / backup, "rb") as fh:
                os.fsync(fh.fileno())
            _sync_dir(self.journal)
            backup_sha256 = _sha256_file(self.journal / backup)

        op = {
            "path": rel,
            "backup": backup,
            "backup_sha256": backup_sha256,
            "directory": bool(directory),
            "temporary": None if directory else self._temporary(index + 1),
        }
        operations.append(op)
        self.save()
        return op

    # ---- mutations ---------------------------------------------------------
    def write(
        self, rel: str, data: bytes | None = None, *, source: str | os.PathLike[str] | None = None
    ) -> None:
        """Replace ``rel`` with ``data`` (or the contents of ``source``)."""
        op = self.record(rel, directory=False)
        target = self._target(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / str(op["temporary"])
        temp.unlink(missing_ok=True)
        try:
            if source is not None:
                shutil.copyfile(str(source), temp)
            else:
                with open(temp, "wb") as fh:
                    fh.write(data or b"")
                    fh.flush()
                    os.fsync(fh.fileno())
            os.chmod(temp, 0o644)
            os.replace(temp, target)
            _sync_dir(target.parent)
        finally:
            temp.unlink(missing_ok=True)

    def mkdir(self, rel: str) -> None:
        self.record(rel, directory=True)
        self._target(rel).mkdir(parents=True, exist_ok=True)

    def remove(self, rel: str) -> None:
        self.record(rel, directory=False)
        self._target(rel).unlink(missing_ok=True)

    # ---- phases ------------------------------------------------------------
    def commit(self) -> None:
        if self.state.get("phase") not in ("applying", "files_verified"):
            raise JournalError("transaction is not in a committable phase")
        self.state["phase"] = "committed"
        self.save()

    def verify(self) -> None:
        """Mark the written tree as post-verified (its own terminal phase)."""
        if self.state.get("phase") != "applying":
            raise JournalError("transaction is not in the applying phase")
        self.state["phase"] = "files_verified"
        self.save()

    def rollback(self) -> None:
        """Undo every recorded operation in reverse order."""
        state = self.load(self.root, self.journal)
        if state.get("phase") != "applying":
            raise JournalError("transaction is not recoverable")
        self.state = state
        operations = state["operations"]
        for op in reversed(operations):
            rel = op["path"]
            target = self._target(rel)
            if op.get("backup") is not None:
                backup = self.journal / str(op["backup"])
                if _sha256_file(backup) != op.get("backup_sha256"):
                    raise JournalError(f"backup integrity mismatch for {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.parent / f".nadare-restore-{op['backup']}"
                temp.unlink(missing_ok=True)
                shutil.copy2(backup, temp)
                os.replace(temp, target)
            else:
                try:
                    if op.get("directory"):
                        target.rmdir()
                    else:
                        target.unlink(missing_ok=True)
                except OSError:
                    pass
            temp = op.get("temporary")
            if temp:
                (target.parent / str(temp)).unlink(missing_ok=True)
            _sync_dir(target.parent)
        self.state["phase"] = "rolled_back"
        self.save()

    def finalize(self) -> Path:
        """Archive the journal directory and return its new path."""
        state = self.load(self.root, self.journal)
        if state.get("phase") == "applying":
            raise JournalError("roll back the interrupted transaction first")
        history = self.root / f".nadare-update-history-{uuid.uuid4().hex}"
        os.replace(self.journal, history)
        _sync_dir(self.root)
        self._prune_history()
        return history

    def _prune_history(self) -> None:
        """Keep only the newest :data:`KEEP_HISTORY` archived journals."""
        try:
            histories = sorted(
                self.root.glob(".nadare-update-history-*"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            )
        except OSError:  # pragma: no cover - a racing/vanished directory
            return
        for old in histories[KEEP_HISTORY:]:
            shutil.rmtree(old, ignore_errors=True)

    # ---- loading / recovery ------------------------------------------------
    @classmethod
    def load(
        cls, root: str | os.PathLike[str], journal_dir: str | os.PathLike[str] | None = None
    ) -> dict[str, Any]:
        root_p = Path(root).expanduser()
        journal = Path(journal_dir) if journal_dir else root_p / JOURNAL_NAME
        journal_file = journal / "journal.json"
        try:
            state = json.loads(journal_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise JournalError(f"no journal at {journal_file}") from exc
        except Exception as exc:  # noqa: BLE001
            raise JournalError(f"cannot read journal: {exc}") from exc
        if not isinstance(state, dict) or state.get("phase") not in PHASES:
            raise JournalError("invalid update journal")
        operations = state.get("operations")
        if not isinstance(operations, list):
            raise JournalError("invalid journal operations")
        if len(operations) > MAX_OPERATIONS:
            raise JournalError("too many journal operations")
        seen: set[str] = set()
        for index, op in enumerate(operations):
            if not isinstance(op, dict):
                raise JournalError("invalid journal operation")
            rel = safe_relative(str(op.get("path", "")))
            if rel in seen:
                raise JournalError(f"duplicate journal path: {rel}")
            seen.add(rel)
            directory = bool(op.get("directory"))
            expected_tmp = None if directory else cls._temporary(index + 1)
            if op.get("temporary") != expected_tmp:
                raise JournalError(f"invalid temporary for {rel}")
            backup = op.get("backup")
            if backup is not None:
                if str(backup) != str(index):
                    raise JournalError(f"invalid backup index for {rel}")
                backup_path = journal / str(backup)
                if not backup_path.is_file() or _sha256_file(backup_path) != op.get(
                    "backup_sha256"
                ):
                    raise JournalError(f"backup integrity mismatch for {rel}")
        return state

    @classmethod
    def recover(
        cls, root: str | os.PathLike[str], journal_dir: str | os.PathLike[str] | None = None
    ) -> dict[str, Any] | None:
        """Finish an interrupted transaction. Returns its final state, or None.

        ``applying`` is rolled back; ``files_verified``/``committed`` are left in
        place for the caller to :meth:`finalize`.
        """
        root_p = Path(root).expanduser()
        journal = Path(journal_dir) if journal_dir else root_p / JOURNAL_NAME
        if not (journal / "journal.json").is_file():
            return None
        state = cls.load(root_p, journal)
        if state["phase"] == "applying":
            tx = cls(root_p, journal)
            tx.rollback()
            return tx.state
        return state


def _sync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
