# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Apply and remove a backend's declared overlays (see :mod:`nadare.core.patchspecs`).

The glue between a game backend and :mod:`nadare.core.patches`: where the install is, where
the pinned archive is cached, and the plain-language errors the game page and the CLI show.
"""

from __future__ import annotations

import logging
import zipfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from . import download, patches, patchspecs, paths
from .backends import BackendError

if TYPE_CHECKING:
    from .backends import GameBackend

log = logging.getLogger(__name__)


def offered(backend: GameBackend) -> tuple[patches.PatchSpec, ...]:
    """The overlays this game offers (empty for nearly every game)."""
    if not callable(getattr(backend, "install_dir", None)):
        return ()
    return patchspecs.specs_for(str(getattr(backend, "kind", "") or ""))


def _spec(
    backend: GameBackend, patch_id: str, *, refresh: bool = False
) -> patches.PatchSpec:
    if refresh:
        if callable(getattr(backend, "install_dir", None)):
            specs = patchspecs.refresh(str(getattr(backend, "kind", "") or ""))
        else:
            specs = ()
    else:
        specs = offered(backend)
    for spec in specs:
        if spec.id == patch_id:
            return spec
    raise BackendError(f"this game has no patch called {patch_id!r}")


def _root(backend: GameBackend) -> Path:
    root = Path(backend.install_dir())  # type: ignore[attr-defined]
    if not root.is_dir():
        raise BackendError("the game is not installed")
    return root.resolve()


def _archive_path(spec: patches.PatchSpec) -> Path:
    return (
        paths.cache_dir()
        / "patches"
        / f"{spec.id}-{spec.version}-{spec.archive_sha256[:16]}.zip"
    )


def _integrity_message(spec: patches.PatchSpec) -> str:
    return (
        f"The {spec.title} download has changed since it was last checked, so it was not "
        "applied. Try again later once the new upload has been checked and pinned."
    )


def _ensure_archive(backend: GameBackend, spec: patches.PatchSpec) -> Path:
    archive = _archive_path(spec)
    if archive.is_file():
        try:
            patches._verify_archive(archive, spec)
        except patches.PatchError:
            archive.unlink(missing_ok=True)
    if not archive.is_file():
        archive.parent.mkdir(parents=True, exist_ok=True)
        backend.info(f"Downloading {spec.title} {spec.version}")
        download.download(
            spec.url,
            archive,
            expected_sha256=spec.archive_sha256,
            expected_size=spec.archive_size,
            emit=backend.ctx.emit,
            cancel=backend.ctx.cancel,
        )
    return archive


def record_for(backend: GameBackend, patch_id: str) -> patches.PatchRecord | None:
    """The persisted record for ``patch_id``, or ``None``. Reads one small file, no hashing."""
    try:
        for record in patches.applied_patches(_root(backend)):
            if record.id == patch_id:
                return record
    except (BackendError, patches.PatchError, OSError):
        return None
    return None


def _record_in(root: Path, patch_id: str) -> patches.PatchRecord | None:
    return next((r for r in patches.applied_patches(root) if r.id == patch_id), None)


def _still_patched(root: Path, entry: patches.PatchFileState) -> bool:
    try:
        return patches._still_patched(root, entry)
    except patches.PatchError:
        return False


def _all_still_patched(root: Path, record: patches.PatchRecord) -> bool:
    return bool(record.files) and all(_still_patched(root, entry) for entry in record.files)


def _any_still_patched(root: Path, record: patches.PatchRecord) -> bool:
    return any(_still_patched(root, entry) for entry in record.files)


def _newer_than(record: patches.PatchRecord, spec: patches.PatchSpec) -> bool:
    """Whether ``spec`` is a newer index pin than what ``record`` applied.

    A built-in fallback object (see :func:`patchspecs.is_builtin_fallback`) is never treated as
    newer, so offline fallback never offers a downgrade. An index document always builds a new
    :class:`PatchSpec` object, so a newer release bumping the built-in pin still offers Update
    when the index document is present.
    """
    return (
        record.archive_sha256 != spec.archive_sha256
        and not patchspecs.is_builtin_fallback(spec)
    )


def build_mismatch(
    backend: GameBackend, spec: patches.PatchSpec
) -> tuple[str, str] | None:
    """``(made for, installed)`` when ``spec`` names a game build and the install is another.

    Lists folders only (no hashing), so the game page may call it. ``None`` when either build
    is unknown or they agree (#1895).
    """
    if spec.game_build is None:
        return None
    try:
        root = _root(backend)
    except BackendError:
        return None
    installed = patchspecs.installed_build(str(getattr(backend, "kind", "") or ""), root)
    if installed is None or installed == spec.game_build:
        return None
    return spec.game_build, installed


def _build_refusal(spec: patches.PatchSpec, wanted: str, installed: str) -> BackendError:
    return BackendError(
        f"{spec.title} {spec.version} is made for game build {wanted}, and this game is now "
        f"build {installed} (the game updated itself). It was not applied: wait for a version "
        f"made for build {installed}."
    )


def page_note(backend: GameBackend, spec: patches.PatchSpec) -> str:
    """One line for the game page when the game moved past the patch's build, else ``""``."""
    mismatch = build_mismatch(backend, spec)
    if mismatch is None:
        return ""
    wanted, installed = mismatch
    return (
        f"The game updated itself to build {installed}. The English patch is made for build "
        f"{wanted}, so it is not active; it can go back on once a version for build "
        f"{installed} is out."
    )


def is_outdated(backend: GameBackend, spec: patches.PatchSpec) -> bool:
    """True when the applied upload is older than the current index pin for ``spec.id``."""
    record = record_for(backend, spec.id)
    return record is not None and _newer_than(record, spec)


def current(backend: GameBackend, patch_id: str) -> patches.PatchSpec:
    """``patch_id``'s spec with the index asked first (falls back to the cache offline)."""
    return _spec(backend, patch_id, refresh=True)


def state(
    backend: GameBackend, patch_id: str, *, spec: patches.PatchSpec | None = None
) -> str:
    """``applied``, ``outdated``, ``undone``, ``wrong-build``, ``pristine``, ``partial``,
    ``unknown`` or ``missing``.

    ``undone``: recorded as applied, but the game moved to another build or rewrote the patched
    files itself (a hot update while running, #1894). ``wrong-build``: not applied, and the pin
    is for another game build. ``spec`` is the pin to compare against; by default the cached one
    (no network).
    """
    root = _root(backend)
    if spec is None:
        spec = _spec(backend, patch_id)
    mismatch = build_mismatch(backend, spec)
    try:
        for record in patches.applied_patches(root):
            if record.id == patch_id:
                if mismatch is not None:
                    return "undone"
                if _newer_than(record, spec):
                    return "outdated"
                if record.files and not _all_still_patched(root, record):
                    return "undone"
                if record.archive_sha256 != spec.archive_sha256 and _all_still_patched(
                    root, record
                ):
                    return "applied"
                break
        else:
            if mismatch is not None:
                return "wrong-build"
        return patches.status(root, spec).state
    except (patches.PatchError, OSError):
        return "unknown"


def status_line(backend: GameBackend, spec: patches.PatchSpec) -> str:
    """``<id> <version> <state>``, plus both builds when the game is on another one (CLI)."""
    line = f"{spec.id} {spec.version} {state(backend, spec.id, spec=spec)}"
    mismatch = build_mismatch(backend, spec)
    if mismatch is not None:
        line += f" (made for game build {mismatch[0]}; installed build {mismatch[1]})"
    return line


def recorded(backend: GameBackend) -> tuple[str, ...]:
    """The ids recorded as applied. Reads one small file, so a page may call it."""
    try:
        return tuple(record.id for record in patches.applied_patches(_root(backend)))
    except (BackendError, patches.PatchError, OSError):
        return ()


def _build_matches(
    root: Path, spec: patches.PatchSpec, record: patches.PatchRecord
) -> tuple[bool, bool]:
    """Check whether the install's underlying build matches ``spec``'s before pins.

    Returns ``(matches, still_applied)``. Never modifies files or downloads anything.
    """
    by_dest = {entry.dest: entry for entry in record.files}
    patched_cache: dict[str, bool] = {}

    def is_patched(entry: patches.PatchFileState) -> bool:
        if entry.dest not in patched_cache:
            patched_cache[entry.dest] = _still_patched(root, entry)
        return patched_cache[entry.dest]

    matches = True
    for item in spec.files:
        if item.before_sha256 is None:
            continue
        entry = by_dest.get(item.dest)
        if entry is not None and is_patched(entry):
            actual_sha, actual_size = entry.pre_sha256, entry.pre_size
        else:
            candidate = root / item.dest
            if not candidate.exists() and not candidate.is_symlink():
                matches = False
                break
            try:
                target = patches._target_path(root, item.dest)
                if not target.is_file():
                    matches = False
                    break
                actual_sha, actual_size = patches._hash_file(target)
            except patches.PatchError:
                matches = False
                break
        if (actual_sha, actual_size) != (item.before_sha256, item.before_size):
            matches = False
            break

    if matches:
        return True, False
    still_applied = bool(record.files) and all(is_patched(entry) for entry in record.files)
    return False, still_applied


def _update(
    backend: GameBackend,
    spec: patches.PatchSpec,
    root: Path,
    record: patches.PatchRecord,
) -> None:
    if not _newer_than(record, spec):
        backend.info(f"{spec.title} is already up to date")
        return
    try:
        matches, still_applied = _build_matches(root, spec, record)
        if not matches:
            if still_applied:
                raise BackendError(
                    f"The newer {spec.title} is made for a different game build; update the game "
                    "first. Your current patch stays applied."
                )
            raise BackendError(
                f"The newer {spec.title} is made for a different game build, so it was not "
                "applied. Update the game first, or wait for a patch for this game build."
            )
        # Download and verify the new archive before touching the installed files.
        archive = _ensure_archive(backend, spec)
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except download.IntegrityError as exc:
        log.warning("patch archive does not match its pin: %s", exc)
        raise BackendError(_integrity_message(spec)) from exc
    except download.DownloadError as exc:
        raise BackendError(f"could not download {spec.title}: {exc}") from exc
    except OSError as exc:
        raise BackendError(f"Could not update {spec.title}: {exc.strerror or exc}") from exc

    try:
        patches.revert(root, spec.id, keep_backups=False)
        if patches.status(root, spec).state != "pristine":
            raise BackendError(
                f"{spec.title} could not be updated because the game files changed while "
                "updating. The patch was not applied."
            )
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except OSError as exc:
        raise BackendError(f"Could not update {spec.title}: {exc.strerror or exc}") from exc

    backend.info(f"Applying {spec.title}")
    try:
        patches.apply(root, spec, archive)
    except patches.PatchRollbackError as exc:
        raise BackendError(
            f"Could not apply the updated {spec.title}, and not every original file could be "
            "put back. Use Repair files to restore the game."
        ) from exc
    except patches.PatchVerificationError as exc:
        archive.unlink(missing_ok=True)
        raise BackendError(
            f"Could not apply the updated {spec.title} ({exc}). "
            "The original game files are still in place."
        ) from exc
    except patches.PatchError as exc:
        raise BackendError(
            f"Could not apply the updated {spec.title} ({exc}). "
            "The original game files are still in place."
        ) from exc
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
        archive.unlink(missing_ok=True)
        raise BackendError(
            f"Could not apply the updated {spec.title} (damaged zip archive). "
            "The original game files are still in place."
        ) from exc
    except OSError as exc:
        raise BackendError(
            f"Could not apply the updated {spec.title}: {exc.strerror or exc}. "
            "The original game files are still in place."
        ) from exc
    backend.info(f"{spec.title} updated. A game update or Repair files undoes it.")


def _refuse_other_build(backend: GameBackend, spec: patches.PatchSpec) -> None:
    mismatch = build_mismatch(backend, spec)
    if mismatch is not None:
        raise _build_refusal(spec, *mismatch)


def apply(backend: GameBackend, patch_id: str) -> None:
    spec = _spec(backend, patch_id, refresh=True)
    root = _root(backend)
    _refuse_other_build(backend, spec)
    try:
        patches.recover(root)
        record = _record_in(root, patch_id)
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except OSError as exc:
        raise BackendError(f"Could not apply {spec.title}: {exc.strerror or exc}") from exc
    stale_record = False
    if record is not None:
        if _newer_than(record, spec):
            _update(backend, spec, root, record)
            return
        try:
            if _all_still_patched(root, record):
                backend.info(f"{spec.title} is already applied")
                return
            stale_record = not _any_still_patched(root, record)
        except OSError as exc:
            raise BackendError(f"Could not apply {spec.title}: {exc.strerror or exc}") from exc
        if not stale_record:
            raise BackendError(
                f"Part of {spec.title} was undone by the game. Turn the patch off first, then "
                "apply it again."
            )
    try:
        current = patches.status(root, spec).state
        if current == "applied":
            if stale_record:
                patches.revert(root, patch_id, keep_backups=False)
            backend.info(
                f"{spec.title} is already in place, applied outside Nadare. Nadare has no "
                "backup of the original files, so Repair files is the way to remove it."
            )
            return
        if current in ("unknown", "missing", "partial"):
            raise BackendError(
                f"{spec.title} {spec.version} was made for another build of this game, so it "
                "was not applied. Wait for the patch to be updated for this build."
            )
        if stale_record:
            patches.revert(root, patch_id, keep_backups=False)
        archive = _ensure_archive(backend, spec)
        backend.info(f"Applying {spec.title}")
        try:
            patches.apply(root, spec, archive)
        except patches.PatchRollbackError as exc:
            raise BackendError(
                f"Could not apply {spec.title}, and not every original file could be put back. "
                "Use Repair files to restore the game."
            ) from exc
        except patches.PatchVerificationError:
            archive.unlink(missing_ok=True)  # a bad cached copy must not be used again
            raise
        except patches.PatchError:
            raise
        except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
            archive.unlink(missing_ok=True)
            raise BackendError(
                f"Could not unpack {spec.title} (damaged zip archive). "
                "The original game files are still in place."
            ) from exc
        except OSError as exc:
            raise BackendError(
                f"Could not apply {spec.title}: {exc.strerror or exc}. "
                "The original game files are still in place."
            ) from exc
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except download.IntegrityError as exc:
        log.warning("patch archive does not match its pin: %s", exc)
        raise BackendError(_integrity_message(spec)) from exc
    except download.DownloadError as exc:
        raise BackendError(f"could not download {spec.title}: {exc}") from exc
    except OSError as exc:
        raise BackendError(f"Could not apply {spec.title}: {exc.strerror or exc}") from exc
    backend.info(f"{spec.title} applied. A game update or Repair files undoes it.")


def update(backend: GameBackend, patch_id: str) -> None:
    """Update an already-applied patch to the latest pinned upload."""
    spec = _spec(backend, patch_id, refresh=True)
    root = _root(backend)
    try:
        patches.recover(root)
        record = _record_in(root, patch_id)
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except OSError as exc:
        raise BackendError(f"Could not update {spec.title}: {exc.strerror or exc}") from exc
    if record is None:
        raise BackendError(f"{spec.title} is not applied")
    _refuse_other_build(backend, spec)
    _update(backend, spec, root, record)


def remove(backend: GameBackend, patch_id: str) -> None:
    spec = _spec(backend, patch_id)
    root = _root(backend)
    try:
        patches.recover(root)
        if patch_id not in recorded(backend):
            if patches.status(root, spec).state == "applied":
                raise BackendError(
                    f"{spec.title} was applied outside Nadare, so there is no backup to restore. "
                    "Use Repair files to get the original files back."
                )
            raise BackendError(f"{spec.title} is not applied")
        patches.revert(root, patch_id, keep_backups=False)
    except patches.PatchError as exc:
        raise BackendError(str(exc)) from exc
    except OSError as exc:
        raise BackendError(f"Could not remove {spec.title}: {exc.strerror or exc}") from exc
    backend.info(f"{spec.title} removed; the original files are back")


def warn_if_undone(backend: GameBackend) -> bool:
    """After an update or a repair: say so when it undid an applied overlay."""
    specs = offered(backend)
    if not specs:
        return False
    try:
        undone = patches.stale(_root(backend), specs)
    except (BackendError, patches.PatchError, OSError):
        log.debug("could not check the applied patches", exc_info=True)
        return False
    for spec in undone:
        backend.info(
            f"{spec.title} was undone by this update or repair. Apply it again from the game "
            "page (only when the patch supports the installed build)."
        )
    return bool(undone)


__all__ = [
    "apply",
    "build_mismatch",
    "current",
    "is_outdated",
    "offered",
    "page_note",
    "record_for",
    "recorded",
    "remove",
    "state",
    "status_line",
    "update",
    "warn_if_undone",
]
