# SPDX-License-Identifier: GPL-3.0-or-later
"""Kuro update patches: download only what changed (#1252).

Kuro's index offers, per resource pack, a ``patchConfig`` entry for each version it can update
from. Its ``indexFile`` lists:

* ``groupInfos``: ``.krpdiff`` patches (see :mod:`krdiff`), each turning some old files
  (``srcFiles``, with md5) into new ones (``dstFiles``);
* ``resource``: the files to download (the ``.krpdiff`` files, plus whole files);
* ``deleteFiles``: files the new version no longer has.

This module is only a speed-up in front of the normal whole-file sync. A group is applied only
when its old files are exactly the ones it was made for; its outputs are built in a staging
folder and must match the md5 the game's own (full) index gives before they replace anything.
Any group that cannot be applied is skipped, and the whole-file sync that always runs afterwards
fetches whatever is still missing or different, exactly as before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from . import directdl, diskspace, krdiff
from .events import Cancel, Cancelled, Event, emitter

log = logging.getLogger(__name__)

#: Key in ``Plan.extra``: ``{"root": cdn_root, "packs": {pack name: patchConfig list}}``.
EXTRA_KEY = "kuro_patches"
#: Staging folder (inside the install's state folder, so a rename is never a copy).
STAGING = "kuro-patch"
#: Free space kept on top of what a group needs.
SPARE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class FileRef:
    path: str
    md5: str
    size: int


@dataclass(frozen=True)
class Group:
    patch: directdl.RemoteFile
    sources: tuple[FileRef, ...]
    outputs: tuple[FileRef, ...]


@dataclass(frozen=True)
class PatchPlan:
    groups: tuple[Group, ...] = ()
    deletes: tuple[str, ...] = ()
    #: The publisher's own update size from the installed version (0 when it gives none).
    publisher_bytes: int = 0
    #: Whole files in the patch lists that are not a group's patch file, in list order.
    wholes: tuple[str, ...] = ()
    #: True only when every pack had a patch entry for its installed version.
    found: bool = False

    @property
    def download_bytes(self) -> int:
        return sum(int(g.patch.size or 0) for g in self.groups)


@dataclass
class Outcome:
    applied: int = 0
    skipped: int = 0
    saved_bytes: int = 0
    downloaded_bytes: int = 0
    notes: list[str] = field(default_factory=list)


def installed_versions(record: str, packs: list[str]) -> dict[str, str]:
    """Each pack's installed version from a recorded plan version ("3.7.2+hd-3.7.3")."""
    base, *moved = str(record or "").split("+")
    versions = dict.fromkeys(packs, base)
    for item in moved:
        name, _, version = item.rpartition("-")
        if name in versions and version:
            versions[name] = version
    return versions


def _https(url: str) -> str:
    if urlparse(url).scheme != "https":
        raise directdl.DirectError(f"refusing a non-https URL: {url!r}")
    return url


def _ref(item: Any) -> FileRef:
    if not isinstance(item, Mapping):
        raise directdl.DirectError("a patch file entry is not an object")
    try:
        return FileRef(
            directdl.safe_relative_path(str(item["dest"])), str(item["md5"]).lower(),
            int(item["size"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise directdl.DirectError("a patch file entry is incomplete") from exc


def prepare(
    plan: directdl.Plan,
    installed: str,
    fetch: Callable[[str, Mapping[str, str] | None], bytes],
) -> PatchPlan:
    """The patches that update an install recorded as ``installed`` to ``plan``.

    Empty when the index offers none for that version. A damaged patch list raises
    :class:`directdl.DirectError` (the caller then simply syncs whole files).
    """
    from .sources.kuro import _json

    info = plan.extra.get(EXTRA_KEY) if isinstance(plan.extra, Mapping) else None
    if not isinstance(info, Mapping):
        return PatchPlan()
    root = _https(str(info.get("root") or ""))
    packs = info.get("packs")
    if not isinstance(packs, Mapping):
        return PatchPlan()
    versions = installed_versions(installed, list(packs))
    current = {rf.path for rf in plan.files}
    groups: list[Group] = []
    deletes: list[str] = []
    wholes: list[str] = []
    seen_wholes: set[str] = set()
    publisher = 0
    found = bool(packs)
    for name, configs in packs.items():
        entry = next(
            (c for c in configs or () if isinstance(c, Mapping)
             and str(c.get("version") or "") == versions.get(name)),
            None,
        )
        if entry is None:
            found = False
            continue
        try:
            publisher += int(entry.get("size") or 0)
        except (TypeError, ValueError):
            pass
        try:
            index_file = str(entry["indexFile"])
            base_url = str(entry["baseUrl"]).rstrip("/") + "/"
        except KeyError as exc:
            raise directdl.DirectError(f"patch entry is missing {exc.args[0]!r}") from exc
        listing = _json(fetch(_https(root + quote(index_file, safe="/")), None), "patch list")
        resources: dict[str, FileRef] = {}
        for item in listing.get("resource") or ():
            ref = _ref(item)
            resources[ref.path] = ref
        patch_dests: set[str] = set()
        for info_group in listing.get("groupInfos") or ():
            if not isinstance(info_group, Mapping):
                raise directdl.DirectError("a patch group is not an object")
            dest = directdl.safe_relative_path(str(info_group.get("dest") or ""))
            patch = resources.get(dest)
            if patch is None:
                raise directdl.DirectError(f"patch {dest} is not in the patch's file list")
            patch_dests.add(dest)
            groups.append(
                Group(
                    directdl.RemoteFile(
                        path=dest, url=_https(root + quote(base_url + dest, safe="/")),
                        size=patch.size, md5=patch.md5,
                    ),
                    tuple(_ref(i) for i in info_group.get("srcFiles") or ()),
                    tuple(_ref(i) for i in info_group.get("dstFiles") or ()),
                )
            )
        for path in resources:
            if path not in patch_dests and path not in seen_wholes:
                seen_wholes.add(path)
                wholes.append(path)
        for item in listing.get("deleteFiles") or ():
            path = directdl.safe_relative_path(str(item))
            if path not in current:  # never a file the new version still lists
                deletes.append(path)
    return PatchPlan(
        tuple(groups), tuple(dict.fromkeys(deletes)), publisher,
        tuple(wholes), found,
    )


def predownload_files(patches: PatchPlan, plan: directdl.Plan) -> list[directdl.RemoteFile]:
    """The patch files and whole files an update from this install will need."""
    by_path = {rf.path: rf for rf in plan.files}
    files: list[directdl.RemoteFile] = []
    seen: set[str] = set()
    for group in patches.groups:
        if group.patch.path not in seen:
            seen.add(group.patch.path)
            files.append(group.patch)
    for path in patches.wholes:
        rf = by_path.get(path)
        if rf is not None and path not in seen:
            seen.add(path)
            files.append(rf)
    return files


# ---- applying ---------------------------------------------------------------------------------


def _md5_of(path: Path, cancel: Cancel | None) -> str | None:
    """md5 of a regular file (no link followed), or ``None`` when it is not one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        digest = hashlib.md5()
        while block := handle.read(krdiff.BLOCK):
            if cancel is not None:
                cancel.raise_if_cancelled()
            digest.update(block)
    return digest.hexdigest()


def _matches(root: Path, ref: FileRef, cancel: Cancel | None) -> bool:
    path = root / ref.path
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != ref.size:
        return False
    return _md5_of(path, cancel) == ref.md5


def apply(
    patches: PatchPlan,
    plan: directdl.Plan,
    root: Path | str,
    *,
    emit=None,
    cancel: Cancel | None = None,
    changed=None,
    cache: Path | str | None = None,
) -> Outcome:
    """Apply every group that fits; skip the rest (the whole-file sync picks them up)."""
    say = emitter(emit)
    root = Path(root)
    expected = {rf.path: (rf.md5 or "").lower() for rf in plan.files}
    outcome = Outcome()
    staging = root / directdl.STATE_DIR / STAGING
    shutil.rmtree(staging, ignore_errors=True)
    total = len(patches.groups)
    try:
        for number, group in enumerate(patches.groups, 1):
            if cancel is not None:
                cancel.raise_if_cancelled()
            say(Event("step", f"Applying update patch {number} of {total}"))
            reason = _apply_group(
                group, root, staging, expected, emit, cancel, changed, outcome, cache
            )
            if reason:
                outcome.skipped += 1
                outcome.notes.append(f"{group.patch.path}: {reason}")
                log.info("patch %s skipped: %s", group.patch.path, reason)
            else:
                outcome.applied += 1
                outcome.saved_bytes += max(
                    0, sum(o.size for o in group.outputs) - int(group.patch.size or 0)
                )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return outcome


def _apply_group(
    group, root, staging, expected, emit, cancel, changed, counter=None, cache=None
) -> str:
    """Apply one group; return why it was skipped, or ``""`` when it was applied."""
    outputs = [o for o in group.outputs if expected.get(o.path) == o.md5]
    if len(outputs) != len(group.outputs):
        return "its new files do not match the game's own file list"
    if all(_matches(root, o, cancel) for o in outputs):
        return "already up to date"
    for source in group.sources:
        if not _matches(root, source, cancel):
            return f"{source.path} is not the file the patch was made for"
    need = int(group.patch.size or 0) + sum(o.size for o in outputs) + SPARE_BYTES
    try:
        staging.mkdir(parents=True, exist_ok=True)
        if diskspace.free_bytes(staging) < need:
            return "not enough free space to patch beside the old files"
    except Exception:  # noqa: BLE001 - BackendError/OSError: unknown space means no patch
        return "cannot check the free space"
    work = staging / "group"
    shutil.rmtree(work, ignore_errors=True)
    try:
        patch_file = None
        if cache is not None:
            try:
                cached = directdl.cached_file(cache, group.patch)
                info = os.lstat(cached)
            except (OSError, directdl.DirectError):
                cached = None
            if (
                cached is not None
                and stat.S_ISREG(info.st_mode)
                and directdl._verify_remote(cached, group.patch, cancel=cancel)
            ):
                patch_file = cached
        if patch_file is None:
            patch_file = work / "patch.krpdiff"
            work.mkdir(parents=True)
            directdl._download_remote(group.patch, patch_file, emit=emit, cancel=cancel)
            if counter is not None:
                counter.downloaded_bytes += int(group.patch.size or 0)
        # Reads the game's files in place (never writes there); builds into the staging folder.
        built = krdiff.apply(patch_file, root, work / "new", cancel=cancel)
        if {e for e in built} - {o.path for o in outputs}:
            return "the patch built other files than its list says"
        for output in outputs:
            if built.get(output.path) != output.md5:
                return f"{output.path} did not come out as expected"
        if set(built) != {o.path for o in outputs}:
            return "the patch did not build every file its list names"
        for output in outputs:
            destination = directdl._safe_destination_path(root, output.path, directory=False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(work / "new" / output.path, destination)
            if changed is not None:
                changed.set()
    except Cancelled:
        raise
    except (krdiff.KrDiffError, directdl.DirectError, OSError) as exc:
        return str(exc) or exc.__class__.__name__
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ""


#: How many of Kuro's older patch lists a cleanup may read (newest first).
MAX_CLEANUP_LISTS = 8
#: Never touched by a cleanup: the game's own runtime data and Nadare's state.
_KEEP_PREFIXES = ("Client/Saved/",)


def _version_key(text: str) -> tuple:
    parts = []
    for part in str(text).split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts)


def _leftovers(plan: directdl.Plan, root: Path) -> set[str]:
    """Files on disk that ``plan`` does not list (no link followed, mods and saves excluded)."""
    listed = {rf.path for rf in plan.files}
    found: set[str] = set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        relative_folder = Path(folder).relative_to(root).as_posix()
        # Nadare's state and anything under a "~" folder (where mods go) are never candidates.
        dirs[:] = [
            d for d in dirs
            if not d.startswith("~") and not (relative_folder == "." and d.startswith(".nadare"))
        ]
        for name in files:
            relative = name if relative_folder == "." else f"{relative_folder}/{name}"
            if relative in listed or relative.startswith(_KEEP_PREFIXES):
                continue
            if relative_folder == "." and name.startswith(".nadare"):
                continue  # Nadare's own records (.nadare-version.json, ...): never a game file
            if stat.S_ISREG(os.lstat(os.path.join(folder, name)).st_mode):
                found.add(relative)
    return found


@dataclass(frozen=True)
class RetiredScan:
    """Retired files found on disk, and the list versions that could not be read."""

    names: tuple[str, ...]
    unreadable: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """True when every list that still mattered could be read."""
        return not self.unreadable


def retired_scan(
    plan: directdl.Plan,
    root: Path | str,
    fetch: Callable[[str, Mapping[str, str] | None], bytes],
) -> RetiredScan:
    """Leftover files Kuro's own patch lists say no current version has (#1252).

    Covers installs that updated before Nadare read those lists. Only files on disk that the
    current plan does not list are candidates, and only the ones a Kuro ``deleteFiles`` list
    names are returned; the lists are read newest first, at most :data:`MAX_CLEANUP_LISTS`.
    A broken list does not hide later ones, and its version is remembered so the cleanup can
    try again instead of marking the install done.
    """
    from .sources.kuro import _json

    info = plan.extra.get(EXTRA_KEY) if isinstance(plan.extra, Mapping) else None
    if not isinstance(info, Mapping) or not isinstance(info.get("packs"), Mapping):
        return RetiredScan((), ())
    root = Path(root)
    candidates = _leftovers(plan, root)
    if not candidates:
        return RetiredScan((), ())
    base = _https(str(info.get("root") or ""))
    entries = [
        entry for configs in info["packs"].values() for entry in configs or ()
        if isinstance(entry, Mapping) and entry.get("indexFile")
        and str(entry.get("version") or "") != plan.version
    ]
    entries.sort(key=lambda e: _version_key(str(e.get("version") or "")), reverse=True)
    retired: set[str] = set()
    unreadable: list[str] = []
    for entry in entries[:MAX_CLEANUP_LISTS]:
        try:
            listing = _json(fetch(_https(base + quote(str(entry["indexFile"]), safe="/")), None),
                            "patch list")
        except directdl.DirectError as exc:
            log.info("cleanup: patch list %s unreadable: %s", entry.get("version"), exc)
            version = str(entry.get("version") or "")
            if version:
                unreadable.append(version)
            continue
        for item in listing.get("deleteFiles") or ():
            if isinstance(item, str) and item in candidates:
                retired.add(item)
        if retired >= candidates:
            # Every leftover is explained: an earlier broken list no longer matters.
            unreadable.clear()
            break
    return RetiredScan(tuple(sorted(retired)), tuple(sorted(set(unreadable), key=_version_key)))


def retired_on_disk(
    plan: directdl.Plan,
    root: Path | str,
    fetch: Callable[[str, Mapping[str, str] | None], bytes],
) -> tuple[str, ...]:
    """The names from :func:`retired_scan` (kept for callers that only need those)."""
    return retired_scan(plan, root, fetch).names


def delete_retired(root: Path | str, deletes: tuple[str, ...]) -> int:
    """Remove files Kuro retired; only regular files inside ``root``. Returns how many."""
    root = Path(root)
    removed = 0
    for relative in deletes:
        path = root / directdl.safe_relative_path(relative)
        try:
            # Every folder on the way must be a real folder of the install, not a link out.
            if path.resolve().parent != root.resolve().joinpath(relative).parent:
                continue
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
                removed += 1
        except (OSError, ValueError):
            continue
    return removed


__all__ = [
    "EXTRA_KEY",
    "FileRef",
    "Group",
    "Outcome",
    "PatchPlan",
    "RetiredScan",
    "apply",
    "delete_retired",
    "installed_versions",
    "predownload_files",
    "prepare",
    "retired_on_disk",
    "retired_scan",
]
