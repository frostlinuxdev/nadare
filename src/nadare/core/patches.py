# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Client-file overlays ("patches") with pinned hashes, backups and a trustworthy revert.

A translation or mod overlay replaces game files that the publisher overwrites on the next
update. Done by hand, the install ends up in a state nobody can describe: nobody knows which
files came from where, an interrupted copy leaves a truncated container, and "revert" is a
folder someone happened to keep. This module makes it one declared object:

* :class:`PatchSpec` says, as data, which archive members land on which files and pins the
  SHA-256 and size of each file *before* and *after* the change. A spec that does not match the
  build it is applied to is refused instead of corrupting the install.
* :func:`apply` backs every target up first (reflink where the filesystem supports it), writes
  each new file beside its target and renames it into place, verifies the result, then records
  what it did in ``.nadare-patches/state.json``. A journal written *before* the first write lets
  :func:`recover` undo an interrupted run, so a crash cannot leave a half-patched install.
* :func:`revert` restores from those backups and verifies the restored bytes.

Safety rules, enforced here rather than trusted to callers:

* a destination is the only name that reaches the filesystem, and it must be a plain relative
  path inside the install folder: absolute paths, ``..``, ``.``, empty components, control
  characters and backslashes are refused, as is any target that resolves outside the install
  or is itself a symlink;
* archive member names never become paths -- members are looked up by exact name from the
  spec, so a hostile archive cannot reach anywhere the spec did not name;
* sizes come from the spec, not the archive: a member bigger than declared is refused before
  anything is written, and the stream is cut off if it keeps going;
* the archive's SHA-256 is checked before it is opened; downloading is the caller's job.

This module never touches the network and never edits the Wine prefix: a patch is a set of
files inside the install folder, nothing else.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from . import atomicfile, installlock

log = logging.getLogger(__name__)

STATE_DIR_NAME = ".nadare-patches"
STATE_NAME = "state.json"
JOURNAL_NAME = "journal.json"
BACKUP_DIR_NAME = "backups"
STATE_VERSION = 1

#: ``ioctl`` for "clone these extents" (reflink). Same value on every Linux architecture.
_FICLONE = 0x40049409

_HASH_CHUNK = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GAME_BUILD_RE = re.compile(r"^[0-9]{1,20}$")

FileStateName = Literal["pristine", "applied", "unknown", "missing"]
PatchStateName = Literal["pristine", "applied", "partial", "unknown"]


class PatchError(RuntimeError):
    """A patch could not be applied or reverted; the install is unchanged."""


class PatchBusy(PatchError):
    """Another patch operation holds the install lock."""


class PatchConflict(PatchError):
    """The file on disk is not the one the spec expects (another build or version)."""


class PatchVerificationError(PatchError):
    """The bytes written or restored do not match the pins in the spec."""


class PatchRollbackError(PatchError):
    """Applying a patch failed and rolling back the partial changes also failed."""


# --------------------------------------------------------------------------- validation


def _check_text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if value.strip() != value:
        raise ValueError(f"{what} has leading or trailing whitespace: {value!r}")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise ValueError(f"{what} contains a control character: {value!r}")
    return value


def _check_relative(value: object, what: str) -> str:
    """A relative POSIX path with no empty, ``.`` or ``..`` components."""
    text = _check_text(value, what)
    if "\\" in text or text.startswith("/"):
        raise ValueError(f"{what} must be a relative POSIX path: {text!r}")
    # Check the components as written: PurePosixPath("a//b") and ("./a/b") normalise the
    # offending part away, which is exactly how a path check gets bypassed.
    if any(part in ("", ".", "..") for part in text.split("/")):
        raise ValueError(f"{what} must not contain '.', '..' or empty components: {text!r}")
    return text


def _check_sha256(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{what} must be a lowercase hex SHA-256: {value!r}")
    return value


def _check_size(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer: {value!r}")
    return value


def _optional_sha256(value: object, what: str) -> str | None:
    return None if value is None else _check_sha256(value, what)


def _optional_size(value: object, what: str) -> int | None:
    return None if value is None else _check_size(value, what)


# --------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class PatchFile:
    """One archive member and the file it replaces.

    ``member`` is looked up by exact name in the archive; ``dest`` is relative to the install
    folder. ``sha256``/``size`` describe the file *after* the patch; ``before_sha256``/
    ``before_size`` describe what has to be there *before* it, which is what makes a spec
    build-specific. Leaving those out accepts whatever is installed: usable for a first
    conversion, never for a shipped spec.
    """

    member: str
    dest: str
    sha256: str
    size: int
    before_sha256: str | None = None
    before_size: int | None = None

    def __post_init__(self) -> None:
        _check_relative(self.member, "member")
        _check_relative(self.dest, "dest")
        _check_sha256(self.sha256, "sha256")
        _check_size(self.size, "size")
        _optional_sha256(self.before_sha256, "before_sha256")
        _optional_size(self.before_size, "before_size")


@dataclass(frozen=True)
class PatchSpec:
    """A declared overlay: where it comes from, what it replaces, what it must produce."""

    id: str
    title: str
    version: str
    url: str
    archive_sha256: str
    files: tuple[PatchFile, ...]
    archive_size: int | None = None
    notes: str = ""
    #: Patches that must go in together (e.g. a container and the string table indexing it:
    #: applying the container alone hung the loading screen, see issue #1866).
    requires: tuple[str, ...] = ()
    #: The game build the before pins come from, when the game names one (Azur Promilia: the
    #: number in its manifest file names). ``None`` = not known; the hashes still decide.
    game_build: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"patch id must match {_ID_RE.pattern}: {self.id!r}")
        _check_text(self.title, "title")
        _check_text(self.version, "version")
        _check_text(self.url, "url")
        _check_sha256(self.archive_sha256, "archive_sha256")
        _optional_size(self.archive_size, "archive_size")
        if not isinstance(self.notes, str):
            raise ValueError("notes must be a string")
        if not self.files:
            raise ValueError(f"patch {self.id!r} declares no files")
        if len({f.dest for f in self.files}) != len(self.files):
            raise ValueError(f"patch {self.id!r} declares a destination twice")
        if len({f.member for f in self.files}) != len(self.files):
            raise ValueError(f"patch {self.id!r} declares an archive member twice")
        if self.game_build is not None and not (
            isinstance(self.game_build, str) and _GAME_BUILD_RE.fullmatch(self.game_build)
        ):
            raise ValueError(f"patch {self.id!r} has a bad game build: {self.game_build!r}")
        for other in self.requires:
            if not _ID_RE.match(other) or other == self.id:
                raise ValueError(f"patch {self.id!r} has a bad requirement: {other!r}")


# --------------------------------------------------------------------------- state


@dataclass(frozen=True)
class PatchFileState:
    """What applying did to one file: enough to undo it byte for byte."""

    dest: str
    member: str
    post_sha256: str
    post_size: int
    #: ``None`` when the destination did not exist before the patch.
    pre_sha256: str | None = None
    pre_size: int | None = None
    #: Backup path relative to the state folder; ``None`` for a file that did not exist.
    backup: str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class PatchRecord:
    """One applied patch, as persisted in ``state.json``."""

    id: str
    version: str
    archive_sha256: str
    applied_at: float
    files: tuple[PatchFileState, ...]


@dataclass(frozen=True)
class FileStatus:
    dest: str
    state: FileStateName
    sha256: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class PatchStatus:
    patch_id: str
    state: PatchStateName
    files: tuple[FileStatus, ...]


# --------------------------------------------------------------------------- helpers


def _hash_file(path: Path) -> tuple[str, int]:
    """One pass, both facts: the SHA-256 and the size actually read."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _state_dir(root: Path) -> Path:
    return root / STATE_DIR_NAME


def _require_install_dir(install_dir: str | os.PathLike[str]) -> Path:
    root = Path(install_dir).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PatchError(f"install folder is missing: {root}") from exc
    if not resolved.is_dir():
        raise PatchError(f"install folder is not a directory: {resolved}")
    return resolved


def _target_path(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``, refusing symlinks and anything that escapes it."""
    target = root / rel
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise PatchError(f"cannot resolve the folder for {rel}: {exc}") from exc
    if not parent.is_relative_to(root):
        raise PatchError(f"refusing to write outside the install folder: {rel}")
    if target.is_symlink():
        raise PatchError(f"refusing to replace the symlink {rel}")
    return target


def _copy_with_reflink(source: Path, target: Path) -> None:
    """Clone ``source`` to ``target`` with a reflink when possible, else copy the bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        mode = stat.S_IMODE(os.fstat(source_fd).st_mode)
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            try:
                try:
                    fcntl.ioctl(target_fd, _FICLONE, source_fd)
                except OSError as exc:
                    if exc.errno not in (
                        errno.EXDEV,
                        errno.EOPNOTSUPP,
                        errno.ENOTTY,
                        errno.EINVAL,
                        errno.ENOSYS,
                        errno.EPERM,
                    ):
                        raise
                    # A refused clone leaves the target empty, so copy from the start.
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    os.ftruncate(target_fd, 0)
                    while chunk := os.read(source_fd, _HASH_CHUNK):
                        os.write(target_fd, chunk)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
            os.chmod(target, mode)
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
    finally:
        os.close(source_fd)


def _fsync_folder(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path: Path, payload: object) -> None:
    atomicfile.write(
        path,
        json.dumps(payload, indent=1, sort_keys=True) + "\n",
        sync_dir=True,
        make_parent=True,
    )


def _read_json(path: Path) -> object | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PatchError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PatchError(f"{path} is not valid JSON: {exc}") from exc


def _parse_file_state(raw: object, patch_id: str) -> PatchFileState:
    if not isinstance(raw, dict):
        raise PatchError(f"the patch state for {patch_id!r} is malformed")
    return PatchFileState(
        dest=_check_relative(raw.get("dest"), "dest"),
        member=_check_relative(raw.get("member"), "member"),
        post_sha256=_check_sha256(raw.get("post_sha256"), "post_sha256"),
        post_size=_check_size(raw.get("post_size"), "post_size"),
        pre_sha256=_optional_sha256(raw.get("pre_sha256"), "pre_sha256"),
        pre_size=_optional_size(raw.get("pre_size"), "pre_size"),
        backup=(
            _check_relative(raw["backup"], "backup") if raw.get("backup") is not None else None
        ),
        mode=(_check_size(raw["mode"], "mode") if raw.get("mode") is not None else None),
    )


def _load_state(root: Path) -> dict[str, PatchRecord]:
    payload = _read_json(_state_dir(root) / STATE_NAME)
    if payload is None:
        return {}
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise PatchError("the patch state file is from another version; revert it by hand")
    patches = payload.get("patches")
    if not isinstance(patches, dict):
        raise PatchError("the patch state file is malformed")
    records: dict[str, PatchRecord] = {}
    for patch_id, raw in patches.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
            raise PatchError(f"the patch state for {patch_id!r} is malformed")
        try:
            applied_at = float(raw.get("applied_at", 0.0))
        except (TypeError, ValueError) as exc:
            raise PatchError(f"the patch state for {patch_id!r} is malformed") from exc
        records[patch_id] = PatchRecord(
            id=patch_id,
            version=str(raw.get("version", "")),
            archive_sha256=str(raw.get("archive_sha256", "")),
            applied_at=applied_at,
            files=tuple(_parse_file_state(item, patch_id) for item in raw["files"]),
        )
    return records


def _save_state(root: Path, records: dict[str, PatchRecord]) -> None:
    payload = {
        "version": STATE_VERSION,
        "patches": {
            patch_id: {
                "version": record.version,
                "archive_sha256": record.archive_sha256,
                "applied_at": record.applied_at,
                "files": [asdict(item) for item in record.files],
            }
            for patch_id, record in sorted(records.items())
        },
    }
    _write_json(_state_dir(root) / STATE_NAME, payload)


def _backup_rel(patch_id: str, dest: str) -> str:
    return f"{BACKUP_DIR_NAME}/{patch_id}/{dest}"


# --------------------------------------------------------------------------- journal


def _journal_field(check, value: object, what: str):
    """Validate one journal field, reporting a hand-edited journal as a patch error."""
    try:
        return check(value, what)
    except ValueError as exc:
        raise PatchError(f"the patch journal is malformed: {exc}") from exc


def _journal_entries(files: Sequence[PatchFileState]) -> list[dict[str, object]]:
    return [
        {
            "dest": item.dest,
            "backup": item.backup,
            "pre_sha256": item.pre_sha256,
            "pre_size": item.pre_size,
            "post_sha256": item.post_sha256,
            "post_size": item.post_size,
            "mode": item.mode,
        }
        for item in files
    ]


def _matches_pin(
    digest: str, size: int, expected_sha256: str, expected_size: int | None
) -> bool:
    return digest == expected_sha256 and (expected_size is None or size == expected_size)


def _journal_path(root: Path) -> Path:
    return _state_dir(root) / JOURNAL_NAME


def _write_journal(root: Path, patch_id: str, files: Sequence[PatchFileState], *, revert: bool):
    _write_json(
        _journal_path(root),
        {
            "version": STATE_VERSION,
            "patch": patch_id,
            "reverting": revert,
            "files": _journal_entries(files),
        },
    )


def _undo_entries(root: Path, entries: Sequence[dict[str, object]]) -> None:
    """Restore every entry from its backup, newest first; the journal's whole purpose."""
    for raw in reversed(list(entries)):
        if not isinstance(raw, dict):
            raise PatchError("the patch journal is malformed")
        dest = _journal_field(_check_relative, raw.get("dest"), "dest")
        backup_rel = raw.get("backup")
        backup = None
        if backup_rel is not None:
            # A journal is local data the user can edit, so keep the backup it names inside
            # the state folder: it must never be able to copy some other file over the game.
            candidate = _state_dir(root) / _journal_field(_check_relative, backup_rel, "backup")
            if not candidate.resolve(strict=False).is_relative_to(_state_dir(root).resolve()):
                raise PatchError(
                    f"the journal names a backup outside the state folder: {backup_rel}"
                )
            backup = candidate
        pre_sha256 = _journal_field(_optional_sha256, raw.get("pre_sha256"), "pre_sha256")
        pre_size = _journal_field(_optional_size, raw.get("pre_size"), "pre_size")
        post_sha256 = _journal_field(_optional_sha256, raw.get("post_sha256"), "post_sha256")
        post_size = _journal_field(_optional_size, raw.get("post_size"), "post_size")
        mode = _journal_field(_optional_size, raw.get("mode"), "mode")
        if post_sha256 is not None:
            candidate_target = root / dest
            if (
                not candidate_target.exists()
                and not candidate_target.is_symlink()
                and candidate_target.parent.resolve(strict=False).is_relative_to(root)
            ):
                if backup is not None:
                    log.info("%s is missing; skipping rollback so it is not overwritten", dest)
                continue
        target = _target_path(root, dest)
        if backup is None:
            # The file did not exist before the patch: unlink only when it still holds the
            # patched bytes (or for older journals without post_sha256).
            if post_sha256 is not None:
                if not target.is_file():
                    continue
                if not _matches_pin(*_hash_file(target), post_sha256, post_size):
                    log.info("%s was rewritten by the game; keeping it", dest)
                    continue
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            _fsync_folder(target.parent)
            continue
        if post_sha256 is not None:
            if not target.is_file():
                log.info("%s is missing; skipping rollback so it is not overwritten", dest)
                continue
            target_sha, target_size = _hash_file(target)
            if pre_sha256 is not None and _matches_pin(
                target_sha, target_size, pre_sha256, pre_size
            ):
                continue
            if not _matches_pin(target_sha, target_size, post_sha256, post_size):
                log.info("%s was rewritten by the game; keeping it", dest)
                continue
        if not backup.is_file():
            # The backup may simply never have been made (the run failed that early). If the
            # target still holds its pre-patch bytes there is nothing to undo; otherwise the
            # only honest answer is that we cannot restore it.
            unchanged = False
            if target.is_file() and pre_sha256 is not None:
                unchanged = _hash_file(target) == (pre_sha256, pre_size)
            if unchanged:
                continue
            raise PatchError(
                f"the backup for {dest} is gone ({backup}); restore it from a pristine copy"
            )
        if pre_sha256 is not None:
            digest, size = _hash_file(backup)
            if (digest, size) != (pre_sha256, pre_size):
                raise PatchVerificationError(f"the backup for {dest} is corrupted")
        temp = target.parent / f".nadare-restore-{uuid.uuid4().hex}"
        try:
            _copy_with_reflink(backup, temp)
            if mode is not None:
                os.chmod(temp, mode)
            os.replace(temp, target)
            _fsync_folder(target.parent)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        if pre_sha256 is not None:
            digest, size = _hash_file(target)
            if (digest, size) != (pre_sha256, pre_size):
                raise PatchVerificationError(f"{dest} did not come back byte for byte")


# --------------------------------------------------------------------------- status


def status(install_dir: str | os.PathLike[str], spec: PatchSpec) -> PatchStatus:
    """Report where each file of ``spec`` stands, without touching anything."""
    root = _require_install_dir(install_dir)
    files: list[FileStatus] = []
    for item in spec.files:
        candidate = root / item.dest
        if (
            not candidate.exists()
            and not candidate.is_symlink()
            and candidate.parent.resolve(strict=False).is_relative_to(root)
        ):
            files.append(FileStatus(item.dest, "missing"))
            continue
        target = _target_path(root, item.dest)
        if not target.exists():
            files.append(FileStatus(item.dest, "missing"))
            continue
        digest, size = _hash_file(target)
        if (digest, size) == (item.sha256, item.size):
            name: FileStateName = "applied"
        elif item.before_sha256 is not None:
            name = (
                "pristine"
                if (digest, size) == (item.before_sha256, item.before_size)
                else "unknown"
            )
        else:
            name = "pristine"
        files.append(FileStatus(item.dest, name, digest, size))
    states = {item.state for item in files}
    if states == {"applied"}:
        overall: PatchStateName = "applied"
    elif states == {"pristine"}:
        overall = "pristine"
    elif "applied" in states:
        overall = "partial"
    else:
        overall = "unknown"
    return PatchStatus(spec.id, overall, tuple(files))


# --------------------------------------------------------------------------- apply


@dataclass
class _Planned:
    item: PatchFile
    target: Path
    pre_sha256: str | None
    pre_size: int | None
    mode: int | None
    backup_rel: str | None

    def as_state(self) -> PatchFileState:
        return PatchFileState(
            dest=self.item.dest,
            member=self.item.member,
            post_sha256=self.item.sha256,
            post_size=self.item.size,
            pre_sha256=self.pre_sha256,
            pre_size=self.pre_size,
            backup=self.backup_rel,
            mode=self.mode,
        )


def _verify_archive(archive: Path, spec: PatchSpec) -> None:
    if not archive.is_file():
        raise PatchError(f"the patch archive is missing: {archive}")
    actual = archive.stat().st_size
    if spec.archive_size is not None and actual != spec.archive_size:
        raise PatchVerificationError(
            f"the patch archive is {actual} bytes, expected {spec.archive_size}"
        )
    digest, _ = _hash_file(archive)
    if digest != spec.archive_sha256:
        raise PatchVerificationError(
            f"the patch archive does not match {spec.id} (sha256 {digest[:12]}...)"
        )


def _extract_member(
    archive: zipfile.ZipFile, item: PatchFile, target: Path, mode: int = 0o644
) -> None:
    """Write one member to ``target`` atomically, cut off at the declared size."""
    try:
        info = archive.getinfo(item.member)
    except KeyError:
        raise PatchError(f"the archive has no member {item.member!r}") from None
    if info.is_dir():
        raise PatchError(f"{item.member!r} is a directory in the archive")
    if info.file_size > item.size:
        raise PatchError(
            f"{item.member!r} is {info.file_size} bytes in the archive, "
            f"more than the declared {item.size}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".nadare-patch-{uuid.uuid4().hex}"
    try:
        with archive.open(info, "r") as source, open(temp, "wb") as sink:
            written = 0
            while chunk := source.read(_HASH_CHUNK):
                written += len(chunk)
                if written > item.size:
                    raise PatchVerificationError(
                        f"{item.member!r} is larger than the declared {item.size} bytes"
                    )
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if written != item.size:
            raise PatchVerificationError(
                f"{item.member!r} is {written} bytes, expected {item.size}"
            )
        digest, size = _hash_file(temp)
        if (digest, size) != (item.sha256, item.size):
            raise PatchVerificationError(
                f"{item.member!r} does not match its pin (sha256 {digest[:12]}...)"
            )
        os.chmod(temp, mode)
        os.replace(temp, target)
        _fsync_folder(target.parent)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def apply(
    install_dir: str | os.PathLike[str],
    spec: PatchSpec,
    archive: str | os.PathLike[str],
    *,
    force: bool = False,
) -> PatchRecord:
    """Apply ``spec`` from ``archive``; on any failure the install is left as it was.

    ``force`` skips the "before" hash check, which is only useful when a *different* overlay is
    already installed. The backup, the size/before-open checks and the post-write verification
    all still happen.
    """
    root = _require_install_dir(install_dir)
    archive_path = Path(archive).expanduser()
    _verify_archive(archive_path, spec)
    with installlock.hold(
        root,
        PatchError,
        f"another patch operation is already running in {root}",
        busy=PatchBusy,
    ):
        records = _load_state(root)
        record = records.get(spec.id)
        if record is not None:
            if (
                status(root, spec).state == "applied"
                and record.archive_sha256 == spec.archive_sha256
            ):
                log.info("patch %s is already applied", spec.id)
                return record
            if any(_still_patched(root, entry) for entry in record.files) or (
                record.archive_sha256 != spec.archive_sha256
            ):
                raise PatchConflict(
                    f"{spec.id} {record.version} is already applied; revert it first"
                )
            # A repair or an update rewrote every file: the record is all that is left of it.
            log.info("patch %s was undone by the game; applying it again", spec.id)
            _revert_locked(root, records, spec.id, keep_backups=False)
        owned = {entry.dest for other in records.values() for entry in other.files}
        overlap = sorted({item.dest for item in spec.files} & owned)
        if overlap:
            raise PatchConflict("another patch already owns these files: " + ", ".join(overlap))

        planned: list[_Planned] = []
        for item in spec.files:
            target = _target_path(root, item.dest)
            if target.exists():
                digest, size = _hash_file(target)
                if (digest, size) == (item.sha256, item.size):
                    raise PatchConflict(f"{item.dest} already matches {spec.id}; revert it first")
                if (
                    not force
                    and item.before_sha256 is not None
                    and (digest, size) != (item.before_sha256, item.before_size)
                ):
                    raise PatchConflict(
                        f"{item.dest} is not the build {spec.id} targets (sha256 "
                        f"{digest[:12]}..., expected {item.before_sha256[:12]}...)"
                    )
                planned.append(
                    _Planned(
                        item=item,
                        target=target,
                        pre_sha256=digest,
                        pre_size=size,
                        mode=stat.S_IMODE(target.stat().st_mode),
                        backup_rel=_backup_rel(spec.id, item.dest),
                    )
                )
            else:
                if item.before_sha256 is not None and not force:
                    raise PatchConflict(f"{item.dest} is missing; the install is incomplete")
                planned.append(
                    _Planned(
                        item=item,
                        target=target,
                        pre_sha256=None,
                        pre_size=None,
                        mode=None,
                        backup_rel=None,
                    )
                )

        _state_dir(root).mkdir(parents=True, exist_ok=True)
        if _journal_path(root).exists():
            raise PatchError("a previous patch operation did not finish; run recover() first")
        backup_dir = _state_dir(root) / BACKUP_DIR_NAME / spec.id
        if spec.id not in records and (backup_dir.exists() or backup_dir.is_symlink()):
            ts = int(time.time())
            orphan = backup_dir.parent / f"{spec.id}.orphan-{ts}"
            suffix = 1
            while orphan.exists() or orphan.is_symlink():
                orphan = backup_dir.parent / f"{spec.id}.orphan-{ts}-{suffix}"
                suffix += 1
            backup_dir.rename(orphan)
            log.warning("moved orphan patch backups for %s to %s", spec.id, orphan)
        states = [entry.as_state() for entry in planned]
        _write_journal(root, spec.id, states, revert=False)

        try:
            for entry in planned:
                if entry.backup_rel is None:
                    continue
                backup = _state_dir(root) / entry.backup_rel
                _copy_with_reflink(entry.target, backup)
                digest, size = _hash_file(backup)
                if (digest, size) != (entry.pre_sha256, entry.pre_size):
                    backup.unlink(missing_ok=True)
                    raise PatchVerificationError(
                        f"the backup of {entry.item.dest} is not a faithful copy"
                    )
            with zipfile.ZipFile(archive_path) as archive:
                for entry in planned:
                    _extract_member(archive, entry.item, entry.target, entry.mode or 0o644)
            record = PatchRecord(
                id=spec.id,
                version=spec.version,
                archive_sha256=spec.archive_sha256,
                applied_at=time.time(),
                files=tuple(states),
            )
            # Inside the rollback: a state.json that cannot be saved must not leave patched files
            # with no record behind (_save_state writes atomically, so the old file stays).
            _save_state(root, {**records, spec.id: record})
        except BaseException as orig_exc:
            try:
                _undo_entries(root, _journal_entries(states))
            except Exception as rollback_exc:
                log.error("could not roll back the whole patch: %s", rollback_exc)
                raise PatchRollbackError(
                    f"could not put every original file back after {spec.id} failed "
                    f"({rollback_exc})"
                ) from orig_exc
            shutil.rmtree(backup_dir, ignore_errors=True)
            try:
                _journal_path(root).unlink()
            except OSError:
                pass
            raise

        records[spec.id] = record
        try:
            _journal_path(root).unlink()
        except OSError:
            pass
        log.info("applied patch %s %s to %s", spec.id, spec.version, root)
        return record


# --------------------------------------------------------------------------- revert


def revert(
    install_dir: str | os.PathLike[str],
    patch_id: str,
    *,
    keep_backups: bool = True,
) -> None:
    """Undo an applied patch, verifying every restored file against its pre-patch hash.

    A file the game has rewritten since (an update or a repair) is left as it is: its backup
    belongs to the build the patch was applied to, and putting it back would break the game.
    """
    root = _require_install_dir(install_dir)
    with installlock.hold(
        root,
        PatchError,
        f"another patch operation is already running in {root}",
        busy=PatchBusy,
    ):
        records = _load_state(root)
        if patch_id not in records:
            raise PatchError(f"patch {patch_id!r} is not applied to {root}")
        _revert_locked(root, records, patch_id, keep_backups=keep_backups)


def _still_patched(root: Path, entry: PatchFileState) -> bool:
    try:
        target = _target_path(root, entry.dest)
    except PatchError:
        return False
    if not target.is_file():
        return False
    return _hash_file(target) == (entry.post_sha256, entry.post_size)


def _revert_locked(
    root: Path, records: dict[str, PatchRecord], patch_id: str, *, keep_backups: bool
) -> None:
    """``revert`` once the install lock is held; ``records`` is updated in place."""
    record = records[patch_id]
    if _journal_path(root).exists():
        raise PatchError("a previous patch operation did not finish; run recover() first")
    files = tuple(entry for entry in record.files if _still_patched(root, entry))
    for entry in record.files:
        if entry not in files:
            log.info("%s was rewritten by the game since %s; keeping it", entry.dest, patch_id)
    _write_journal(root, patch_id, files, revert=True)
    try:
        _undo_entries(root, _journal_entries(files))
    except BaseException:
        try:
            _journal_path(root).unlink()
        except OSError:
            pass
        raise
    del records[patch_id]
    _save_state(root, records)
    try:
        _journal_path(root).unlink()
    except OSError:
        pass
    if not keep_backups:
        shutil.rmtree(_state_dir(root) / BACKUP_DIR_NAME / patch_id, ignore_errors=True)
    log.info("reverted patch %s in %s", patch_id, root)


def recover(install_dir: str | os.PathLike[str]) -> list[str]:
    """Undo a patch operation that was interrupted (crash, power loss, kill).

    The journal is written before the first file is touched, so an unfinished operation can
    always be rolled back from the backups it names. Returns the patch ids that were rolled
    back (empty when there was nothing to do).
    """
    root = _require_install_dir(install_dir)
    payload = _read_json(_journal_path(root))
    if payload is None:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("patch"), str):
        raise PatchError(f"{_journal_path(root)} is malformed; inspect it by hand")
    patch_id = payload["patch"]
    entries = payload.get("files")
    if not isinstance(entries, list):
        raise PatchError(f"{_journal_path(root)} is malformed; inspect it by hand")
    with installlock.hold(
        root,
        PatchError,
        f"another patch operation is already running in {root}",
        busy=PatchBusy,
    ):
        _undo_entries(root, entries)
        records = _load_state(root)
        if records.pop(patch_id, None) is not None:
            _save_state(root, records)
        _journal_path(root).unlink(missing_ok=True)
    log.warning("rolled back the interrupted patch %s in %s", patch_id, root)
    return [patch_id]


# --------------------------------------------------------------------------- queries


def applied_patches(install_dir: str | os.PathLike[str]) -> tuple[PatchRecord, ...]:
    """Every patch currently recorded as applied, newest first."""
    root = _require_install_dir(install_dir)
    return tuple(sorted(_load_state(root).values(), key=lambda r: r.applied_at, reverse=True))


def stale(
    install_dir: str | os.PathLike[str], specs: Iterable[PatchSpec]
) -> tuple[PatchSpec, ...]:
    """The specs recorded as applied whose files the game no longer carries (an update or a
    repair rewrote them). Hashes the files, so call it after such an operation, not per probe."""
    root = _require_install_dir(install_dir)
    records = _load_state(root)
    return tuple(
        spec
        for spec in specs
        if spec.id in records
        and (
            not all(_still_patched(root, entry) for entry in records[spec.id].files)
            if records[spec.id].files
            else status(root, spec).state != "applied"
        )
    )


def missing_requirements(
    spec: PatchSpec, records: Iterable[PatchRecord] | None = None
) -> tuple[str, ...]:
    """Requirements of ``spec`` that are not recorded as applied yet."""
    have = {record.id for record in (records if records is not None else ())}
    return tuple(other for other in spec.requires if other not in have)


def ensure_requirements(spec: PatchSpec, records: Iterable[PatchRecord] | None = None) -> None:
    """Raise :class:`PatchConflict` when a required patch is missing."""
    missing = missing_requirements(spec, records)
    if missing:
        raise PatchConflict(f"{spec.id} must be applied together with: " + ", ".join(missing))


__all__ = [
    "FileStatus",
    "PatchBusy",
    "PatchConflict",
    "PatchError",
    "PatchFile",
    "PatchFileState",
    "PatchRecord",
    "PatchRollbackError",
    "PatchSpec",
    "PatchStatus",
    "PatchVerificationError",
    "applied_patches",
    "stale",
    "apply",
    "ensure_requirements",
    "missing_requirements",
    "recover",
    "revert",
    "status",
]
