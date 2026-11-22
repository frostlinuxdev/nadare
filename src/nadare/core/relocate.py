# SPDX-License-Identifier: GPL-3.0-or-later
"""Move a folder (a Wine prefix) to another place without risking the original.

On one disk it is a rename. Across disks the folder is copied next to its destination,
checked against the source, and only then renamed into place. The source is left alone
here; once the game points at the copy the caller removes it with :func:`remove_original`.
Symlinks are copied as links, never followed. ``move_tree`` deletes nothing except the
partial copy it made itself.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diskspace, prefixmove
from .events import Cancel

#: Left free on the destination disk besides the size of the copy.
SPACE_MARGIN = 1 << 30
PARTIAL_SUFFIX = ".nadare-partial"
CHUNK = 1 << 20


class RelocateError(RuntimeError):
    """The move was refused or failed; nothing of the user's was changed."""


@dataclass(frozen=True)
class Moved:
    renamed: bool
    kept_old: Path | None


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def _same_device(src: Path, dst: Path) -> bool:
    return os.stat(src).st_dev == os.stat(_nearest_existing(dst.parent)).st_dev


def _walk_error(exc: OSError) -> None:
    """``os.walk`` hides a folder it cannot list; a move must not, or the copy would "verify"."""
    raise exc


def _summary(root: Path) -> tuple[int, int, int, int, int]:
    """``(files, dirs, links, bytes, newest mtime)`` below ``root``.

    Only regular files, folders and symlinks count: a FIFO, socket or device is never copied.
    """
    files = dirs = links = size = newest = 0
    for current, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        for name in list(names):
            path = Path(current, name)
            if path.is_symlink():
                links += 1
                names.remove(name)  # a link to a directory is still just a link
            else:
                dirs += 1
        for name in filenames:
            info = os.lstat(Path(current, name))
            if stat.S_ISLNK(info.st_mode):
                links += 1
            elif stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
                newest = max(newest, info.st_mtime_ns)
    return files, dirs, links, size, newest


def _inventory(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (bytes, mtime_ns)}`` for every regular file below ``root``.

    The summary's single newest mtime cannot see a same-size file rewritten with a timestamp
    below the newest one in the tree (#915), so the move compares each file.
    """
    found: dict[str, tuple[int, int]] = {}
    for current, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        names[:] = [n for n in names if not Path(current, n).is_symlink()]
        for name in filenames:
            path = Path(current, name)
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                found[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns)
    return found


def _check(src: Path, dst: Path) -> None:
    for label, path in (("move", src), ("destination", dst)):
        if path == Path("/"):
            raise RelocateError(f"Refusing to use the filesystem root as the {label}.")
    if not src.is_dir() or src.is_symlink():
        raise RelocateError(f"{src} is not a folder.")
    reason = prefixmove._refusal_reason(src)  # noqa: SLF001 - same package, same rules
    if reason:
        raise RelocateError(f"Refusing to move {reason}.")
    real_src, real_dst = src.resolve(), dst.resolve()
    if real_dst == real_src or real_src in real_dst.parents:
        raise RelocateError("The new place is inside the folder being moved.")
    if real_dst in real_src.parents:
        raise RelocateError("The new place contains the folder being moved.")
    if dst.exists() or dst.is_symlink():
        raise RelocateError(f"{dst} already exists. Pick a place that does not exist yet.")


def _copy(
    src: Path,
    partial: Path,
    total: int,
    cancel: Cancel | None,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Copy ``src`` into ``partial``, an empty folder the caller just made.

    Returns ``{relative path: sha256}`` of every regular file as it was read, so the caller
    can check the copy's bytes, not only its sizes (#1366 A10).
    """
    done = 0
    digests: dict[str, str] = {}
    dir_stats: list[tuple[Path, Path]] = [(src, partial)]
    for current, names, filenames in os.walk(src, followlinks=False, onerror=_walk_error):
        rel = Path(current).relative_to(src)
        target_dir = partial / rel
        for name in list(names):
            path = Path(current, name)
            if path.is_symlink():
                os.symlink(os.readlink(path), target_dir / name)
                names.remove(name)
            else:
                (target_dir / name).mkdir()
                dir_stats.append((path, target_dir / name))
        for name in filenames:
            path, target = Path(current, name), target_dir / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                os.symlink(os.readlink(path), target)
                continue
            if not stat.S_ISREG(mode):
                continue  # FIFO, socket or device: opening it could block forever
            digest = hashlib.sha256()
            with open(path, "rb") as reader, open(target, "wb") as writer:
                while chunk := reader.read(CHUNK):
                    digest.update(chunk)
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    writer.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            shutil.copystat(path, target)
            digests[path.relative_to(src).as_posix()] = digest.hexdigest()
    # Folder modes last: a read-only source folder must stay writable while it is filled.
    for source_dir, target_dir in reversed(dir_stats):
        shutil.copystat(source_dir, target_dir)
    if progress is not None:
        progress(total, total)
    return digests


def _digests(root: Path) -> dict[str, str]:
    """``{relative path: sha256}`` of every regular file below ``root`` (links not followed)."""
    found: dict[str, str] = {}
    for current, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        names[:] = [n for n in names if not Path(current, n).is_symlink()]
        for name in filenames:
            path = Path(current, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as reader:
                while chunk := reader.read(CHUNK):
                    digest.update(chunk)
            found[path.relative_to(root).as_posix()] = digest.hexdigest()
    return found


def _remove_partial(partial: Path) -> None:
    """Delete the copy this call made, including folders it already made read-only."""

    def make_writable(func, path, exc_info):  # noqa: ANN001 - shutil's callback signature
        try:
            os.chmod(os.path.dirname(path) or ".", 0o700)
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    shutil.rmtree(partial, onexc=make_writable)


def move_tree(
    src: Path,
    dst: Path,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
    journal: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> Moved:
    """Move ``src`` to ``dst`` (which must not exist yet)."""
    src, dst = Path(src), Path(dst)
    _check(src, dst)
    if journal is not None:
        _check_move_identity(journal, src, dst)
    if cancel is not None:
        cancel.raise_if_cancelled()  # a Cancel before anything moved changes nothing (#1366)
    try:
        same_device = _same_device(src, dst)
    except OSError as exc:
        raise RelocateError(f"Could not move {src}: {exc}") from exc
    if same_device:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(dst):
                raise RelocateError(f"{dst} appeared; nothing was replaced.")
            os.rename(src, dst)
            return Moved(renamed=True, kept_old=None)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise RelocateError(f"Could not move {src}: {exc}") from exc
            # A same-device name can still refuse EXDEV (bind mount, btrfs subvolume):
            # fall through to the verified copy path below.

    try:
        before = _summary(src)
        files_before = _inventory(src)
    except OSError as exc:
        raise RelocateError(f"Could not read {src}: {exc}") from exc
    size = before[3]
    try:
        free = diskspace.free_bytes(_nearest_existing(dst.parent))
    except Exception as exc:  # noqa: BLE001 - BackendError/OSError: refuse, do not guess
        raise RelocateError(f"Could not read the free space at {dst.parent}: {exc}") from exc
    if free < size + SPACE_MARGIN:
        raise RelocateError(
            f"Not enough space at {dst.parent}: needs about "
            f"{diskspace.human_size(size + SPACE_MARGIN)}, "
            f"{diskspace.human_size(free)} free."
        )
    try:
        # Only now: a Move refused for space must not leave an empty <library>/Nadare (#1366).
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RelocateError(f"Could not move {src}: {exc}") from exc
    partial = dst.with_name(dst.name + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        raise RelocateError(f"{partial} is left over from an earlier move; remove it first.")
    created = False
    partial_identity = None
    try:
        try:
            partial.mkdir()
        except FileExistsError:  # not ours: never clean it up
            raise RelocateError(f"{partial} appeared while starting; nothing changed.") from None
        created = True
        partial_identity = _identity(partial)
        if journal is not None:
            record_partial(journal, partial)
            if checkpoint is not None:
                checkpoint()  # durable proof of this particular partial before copying
            _check_move_identity(journal, src, dst)
        read = _copy(src, partial, size, cancel, progress)
        after = _summary(src)
        if after != before or _inventory(src) != files_before:
            raise RelocateError(
                "Files changed while they were being copied. Close the game and try again."
            )
        copied = _inventory(partial)
        if _summary(partial)[:4] != before[:4] or {
            name: info[0] for name, info in copied.items()
        } != {name: info[0] for name, info in files_before.items()}:
            raise RelocateError("The copy could not be verified against the original.")
        if _digests(partial) != read:
            # Same names and sizes, other bytes: a bad disk, cable or driver (#1366 A10).
            raise RelocateError(
                "The copy could not be verified against the original: its contents differ. "
                "The original was not touched."
            )
        if os.path.lexists(dst):
            raise RelocateError(f"{dst} appeared while copying; nothing was replaced.")
        if _identity(partial) != partial_identity:
            raise RelocateError("The unfinished copy changed identity; it was kept.")
        if journal is not None:
            _check_move_identity(journal, src, dst)
        os.rename(partial, dst)
    except BaseException as exc:
        if created and partial_identity is not None and _identity(partial) == partial_identity:
            _remove_partial(partial)  # only the copy this call made
        if isinstance(exc, OSError):
            raise RelocateError(f"Could not copy {src}: {exc}") from exc
        raise
    return Moved(renamed=False, kept_old=src)


#: Written by Nadare into the new game folder after the copy (libraries.sync_game): not part
#: of the comparison, or the original of a moved game folder would always be kept (review A4).
_OWN_FILES = frozenset({"nadare-game.json", ".nadare-game.json.tmp"})


def _sizes(root: Path) -> dict[str, int]:
    """Names and sizes, as the move checks its copy (a copy need not keep timestamps)."""
    return {
        name: info[0] for name, info in _inventory(root).items() if name not in _OWN_FILES
    }


def _stamps(root: Path) -> dict[str, tuple[int, int]]:
    return {name: info for name, info in _inventory(root).items() if name not in _OWN_FILES}


#: FAT and exFAT keep modification times in 2 s steps (#1366 A10).
_COARSE_NS = 2_000_000_000


def _written_after_copy(old_ns: int, new_ns: int) -> bool:
    """Whether the original's file is newer than its copy: written after the copy was made.

    The copy keeps each file's time (``copystat``). A newer copy is fine (the game already runs
    from the new place); a newer original holds writes the copy lacks. A coarse disk (FAT,
    exFAT: whole seconds, 2 s steps) rounds the copy's time, so within one step is not newer.
    """
    if old_ns <= new_ns:
        return False
    rounded = new_ns % 1_000_000_000 == 0
    return not (rounded and old_ns - new_ns < _COARSE_NS)


def _same_files(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> bool:
    """Same names and sizes, and no original file written since the copy (#1366 A10).

    The bytes were checked when the copy was made, so a same-size write to the original
    afterwards shows only as a newer time; it must not be deleted with the original.
    """
    if old.keys() != new.keys():
        return False
    return all(
        old[n][0] == new[n][0] and not _written_after_copy(old[n][1], new[n][1]) for n in old
    )


def _links(root: Path) -> dict[str, str]:
    """``{relative path: link text}`` for every symlink below ``root`` (never followed)."""
    found: dict[str, str] = {}
    for current, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        for name in [*names, *filenames]:
            path = Path(current, name)
            if path.is_symlink():
                found[path.relative_to(root).as_posix()] = os.readlink(path)
    return found


def _stays_inside(link: Path, link_text: str, real_root: Path) -> bool:
    """Whether a relative link at ``link`` resolves inside ``real_root`` (its own folder)."""
    try:
        target = Path(os.path.realpath(os.path.join(link.parent, link_text)))
    except (OSError, ValueError):
        return False
    return target == real_root or real_root in target.parents


def _reaches(link: Path, link_text: str, real_root: Path) -> bool:
    """Whether the link at ``link`` leads into ``real_root`` by any route (round 2, C2).

    The spelling can differ (``/home`` vs ``/var/home``, a relative path climbing back), so
    the place it names is resolved, from the link's own folder.
    """
    try:
        target = Path(os.path.realpath(os.path.join(link.parent, link_text)))
    except (OSError, ValueError):
        return True  # cannot tell: keep the original
    return target == real_root or real_root in target.parents


def _points_into(link_text: str, root: Path) -> bool:
    """Whether an absolute link text names ``root`` or something inside it."""
    if not os.path.isabs(link_text):
        return False
    target = Path(os.path.normpath(link_text))
    return target == root or root in target.parents


@dataclass(frozen=True)
class OriginalRemoval:
    removed: bool
    reason: str = ""


def remove_original(old: Path, new: Path) -> OriginalRemoval:
    """Delete ``old`` after a verified copy to ``new`` (Move moves; user, 2026-11-14).

    Call it only once the profile points at ``new``. The original goes only while ``new``
    still matches it file for file (names, kinds and sizes); overlapping folders and a link
    as the original are refused. Mounted drives inside ``old`` are left alone and reported.
    Never raises: the game already runs from ``new``, so a refusal only leaves a copy behind.
    """
    from . import removal  # removal reaches relocate through other modules: import late

    old, new = Path(old), Path(new)
    try:
        if old.is_symlink() or not old.is_dir():
            return OriginalRemoval(False, f"{old} is not a folder Nadare can remove.")
        if new.is_symlink() or not new.is_dir():
            return OriginalRemoval(False, f"{new} is missing, so the original was kept.")
        real_old, real_new = old.resolve(), new.resolve()
        if (
            real_old == real_new
            or real_old in real_new.parents
            or real_new in real_old.parents
        ):
            return OriginalRemoval(False, "The two folders overlap, so nothing was removed.")
        links = _links(new)
        if not _same_files(_stamps(old), _stamps(new)) or _links(old) != links:
            return OriginalRemoval(
                False, f"The copy at {new} does not match the original, so it was kept."
            )
        escaping = [
            name
            for name, text in _links(old).items()
            if not os.path.isabs(text) and not _stays_inside(old / name, text, real_old)
        ]
        if escaping:
            # A relative link that led out of the folder now leads somewhere else from the
            # copy's place: the copy is not the same game (storage review round 3, B-F1).
            return OriginalRemoval(
                False,
                f"{escaping[0]} is a link that leads out of the folder, so the original was kept.",
            )
        inward = [
            name
            for name, text in links.items()
            if _points_into(text, old)
            or _points_into(text, real_old)
            or _reaches(new / name, text, real_old)
        ]
        if inward:
            # A link copied as-is still names the old place: deleting it would leave the copy
            # (and maybe the game's own paths through it) pointing at nothing (review A2).
            return OriginalRemoval(
                False,
                f"{inward[0]} in the copy is a link into the original, so the original was kept.",
            )
        # The copy's bytes may still be only in memory: a power cut after the delete would
        # leave neither (storage review round 2, C1).
        os.sync()
        outcome = removal._rmtree_safely(old)
    except OSError as exc:
        return OriginalRemoval(False, f"Could not remove {old}: {exc}")
    if outcome.mounts or outcome.leftovers:
        left = [str(p) for p in outcome.mounts] + [str(p) for p, _exc in outcome.leftovers]
        return OriginalRemoval(
            False, "Some of the original could not be removed: " + ", ".join(left)
        )
    return OriginalRemoval(True)


#: Originals a Move still has to delete, saved with the new location in one write so a crash
#: in between cannot strand them (storage review round 2, C4): ``[[old, new], ...]``.
PENDING_ORIGINALS = "move_originals_pending"


def with_pending_original(
    changes: dict[str, Any], profile: Mapping[str, Any], old: Path, new: Path
) -> dict[str, Any]:
    """``changes`` plus ``[old, new]`` added to the profile's pending originals."""
    current = profile.get(PENDING_ORIGINALS)
    entries = [list(e) for e in current if _entry(e)] if isinstance(current, list) else []
    entry: list[Any] = [str(old), str(new)]
    identity = _identity(old)
    if identity is not None:
        entry += list(identity)
    entries.append(entry)
    return {**changes, PENDING_ORIGINALS: entries}


def _identity(folder: Path) -> tuple[int, int] | None:
    """``(device, inode)`` of ``folder`` itself (not followed through a link)."""
    try:
        info = os.lstat(folder)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def _entry(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 4):
        return False
    paths_ok = all(isinstance(v, str) and v.strip() and "\x00" not in v for v in value[:2])
    ids_ok = all(isinstance(v, int) and not isinstance(v, bool) for v in value[2:])
    return paths_ok and ids_ok


def _game_places(profile: Mapping[str, Any]) -> list[Path]:
    places: list[Path] = []
    values: list[Any] = [profile.get("prefix"), profile.get("install_dir")]
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list):
        values += dirs
    for value in values:
        if isinstance(value, str) and value.strip() and "\x00" not in value:
            try:
                places.append(Path(os.path.expanduser(value)).resolve())
            except (OSError, ValueError):
                continue
    return places


def pending_originals(config: Any) -> list[tuple[str, Path, Path]]:
    """``(profile, old, new)`` still to delete, only where ``new`` is where the game lives.

    The record is in the config, which anyone can edit: an entry whose copy is not the game's
    own prefix, game folder or game folder home is never acted on.
    """
    found: list[tuple[str, Path, Path]] = []
    for name, profile in config.profiles.items():
        if not isinstance(profile, Mapping):
            continue
        entries = profile.get(PENDING_ORIGINALS)
        if not isinstance(entries, list):
            continue
        places = _game_places(profile)
        for entry in entries:
            if not _entry(entry) or len(entry) != 4:
                continue  # no identity: never acted on (round 3: a stale record is not proof)
            old, new = (Path(os.path.expanduser(v)) for v in entry[:2])
            try:
                # The original must be the very folder the Move copied (same device and
                # inode), not a later backup or a folder a restored config happens to name
                # (storage review round 3, B-F3 / C-F1).
                if new.resolve() in places and _identity(old) == (entry[2], entry[3]):
                    found.append((name, old, new))
            except (OSError, ValueError):
                continue
    return found


def prune_pending_originals(profile: dict[str, Any]) -> bool:
    """Drop entries whose original is gone (or damaged ones); ``True`` when it changed."""
    entries = profile.get(PENDING_ORIGINALS)
    if entries is None:
        return False
    kept = []
    if isinstance(entries, list):
        for entry in entries:
            # Entries without the folder's identity are never acted on: drop them.
            if (
                _entry(entry)
                and len(entry) == 4
                and os.path.lexists(os.path.expanduser(entry[0]))
            ):
                kept.append(list(entry))
    if kept:
        if kept == entries:
            return False
        profile[PENDING_ORIGINALS] = kept
    else:
        profile.pop(PENDING_ORIGINALS, None)
    return True


def forget_pending_original(profile: dict[str, Any], old: Path) -> bool:
    """Drop ``old`` from the profile's pending originals; ``True`` when it changed."""
    entries = profile.get(PENDING_ORIGINALS)
    if not isinstance(entries, list):
        return False
    kept = [list(e) for e in entries if _entry(e) and Path(os.path.expanduser(e[0])) != Path(old)]
    if len(kept) == len(entries):
        return False
    if kept:
        profile[PENDING_ORIGINALS] = kept
    else:
        profile.pop(PENDING_ORIGINALS, None)
    return True


#: A Move in progress, saved before anything is moved (#1366 B-F2): ``{"from": .., "to": ..}``.
#: A crash between the move and the profile save left the game pointing at a folder that was
#: gone; the next start finishes it with :func:`finish_interrupted_moves`.
MOVE_IN_PROGRESS = "move_in_progress"


def begin_move(profile: dict[str, Any], src: Path, dst: Path) -> None:
    """Record a Move about to start (the caller saves the config before moving)."""
    anchor = _nearest_existing(Path(dst).parent).absolute()
    profile[MOVE_IN_PROGRESS] = {
        "from": str(src), "to": str(dst),
        "source_id": list(_identity(Path(src)) or ()),
        "anchor": str(anchor), "anchor_id": list(_identity(anchor.resolve()) or ()),
    }


def end_move(profile: dict[str, Any]) -> bool:
    """Forget the Move record (done, refused or failed); ``True`` when there was one."""
    return profile.pop(MOVE_IN_PROGRESS, None) is not None


def _repoint(value: Any, old: Path, new: Path) -> None:
    """Rewrite stored paths under ``old`` to the same place under ``new``, in place."""
    items = value.items() if isinstance(value, dict) else enumerate(value)
    for key, item in list(items):
        if isinstance(item, str) and item.strip() and "\x00" not in item:
            try:
                path = Path(os.path.expanduser(item))
                if path == old or old in path.parents:
                    value[key] = str(new / path.relative_to(old))
            except (OSError, ValueError):
                continue
        elif isinstance(item, (dict, list)):
            _repoint(item, old, new)


def _record_id(record: Mapping[str, Any], key: str) -> tuple[int, int] | None:
    value = record.get(key)
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        return value[0], value[1]
    return None


def _anchor_matches(record: Mapping[str, Any]) -> bool:
    value = record.get("anchor")
    identity = _record_id(record, "anchor_id")
    try:
        return (isinstance(value, str) and bool(value) and "\x00" not in value
                and identity is not None and _identity(Path(value).resolve()) == identity)
    except (OSError, RuntimeError, ValueError):
        return False  # an unavailable or looping alias is ambiguous, not a startup failure


def _check_move_identity(record: Mapping[str, Any], src: Path, dst: Path) -> None:
    identity = _record_id(record, "source_id")
    if (record.get("from") != str(src) or record.get("to") != str(dst)
            or identity is None or _identity(src) != identity or not _anchor_matches(record)):
        raise RelocateError("A folder or drive changed before the Move; nothing was moved.")


def record_partial(record: dict[str, Any], partial: Path) -> None:
    """Record only the actual partial this Move created, never a guessed name."""
    identity = _identity(partial)
    if identity is None:
        raise RelocateError("The unfinished copy is no longer the folder this Move created.")
    record["partial_id"] = list(identity)


def finish_interrupted_moves(profiles: Mapping[str, Any]) -> list[str]:
    """Recover only with recorded directory identities; ambiguity preserves files and record.

    A rename keeps the source inode. A published copy keeps its recorded partial inode.
    Legacy path-only records cannot prove ownership and are retained for manual recovery.
    """
    notes: list[str] = []
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        record = profile.get(MOVE_IN_PROGRESS)
        if not isinstance(record, Mapping):
            if record is not None:
                profile.pop(MOVE_IN_PROGRESS, None)
            continue
        src_text, dst_text = record.get("from"), record.get("to")
        if not all(isinstance(t, str) and t.strip() and "\x00" not in t
                   for t in (src_text, dst_text)):
            profile.pop(MOVE_IN_PROGRESS, None)
            continue
        src, dst = Path(os.path.expanduser(src_text)), Path(os.path.expanduser(dst_text))
        source_id = _record_id(record, "source_id")
        if source_id is None or not _anchor_matches(record):
            notes.append(
                f"{name}: Move identity cannot be verified; files and recovery record kept."
            )
            continue
        present = _identity(src)
        if present == source_id:
            partial = dst.with_name(dst.name + PARTIAL_SUFFIX)
            partial_id = _record_id(record, "partial_id")
            if partial_id is not None and _identity(partial) == partial_id:
                _remove_partial(partial)
                notes.append(f"{name}: removed its verified unfinished copy at {partial}")
            elif os.path.lexists(partial):
                notes.append(f"{name}: unverified folder at {partial} was kept.")
            if dst.exists():
                notes.append(f"{name}: a copy may be at {dst}; the game still uses {src}.")
            profile.pop(MOVE_IN_PROGRESS, None)
            continue
        copied_id = _record_id(record, "partial_id")
        destination_id = _identity(dst)
        if not os.path.lexists(src) and destination_id is not None and (
                destination_id == source_id
                or (copied_id is not None and destination_id == copied_id)):
            _repoint(profile, src, dst)
            try:
                real_src = src.resolve()
            except OSError:
                real_src = src
            if real_src != src:
                _repoint(profile, real_src, dst)
            profile.pop(MOVE_IN_PROGRESS, None)
            notes.append(f"{name}: finished its verified Move to {dst}")
        else:
            notes.append(
                f"{name}: Move is unresolved; files and recovery record kept "
                "until its drives return."
            )
    return notes
