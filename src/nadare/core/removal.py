# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Remove a game from the library: profile, shortcuts, login and (opt-in) data.

The game's install directory and Wine prefix are configuration paths that may
live anywhere; they are only deleted when explicitly requested via
``delete_trees``, and only after a safety check (see ``removable_trees``). The
profile's own folder under ``paths.profiles_dir()`` can be deleted with
``delete_data``, and only when it is a real directory inside that root (never a
symlink pointing elsewhere).
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import shutil
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import (
    backends,
    gamefacts,
    libraries,
    location,
    paths,
    process,
    runlock,
    runlog,
    shadercache,
    shortcuts,
)

_TREE_KEYS = ("install_dir", "prefix", "wineprefix")
_PENDING_REMOVAL = "removal_pending"
_BACKUP_KEY = "prefix_backup"
#: The names ``prefixmove.backup_path`` makes: ``<prefix name>.broken-<stamp>``, plus ``-2``...
_BACKUP_NAME = re.compile(r"^.+\.broken-\d{8}-\d{6}(?:-\d+)?$")
_BACKUP_NOT_NAMED = "is not named like an old prefix backup, so it is kept"
_BACKUP_IS_LIVE = "is, holds or lies inside this game's current files, so it is kept"


@dataclass
class RemovalResult:
    profile: str
    shortcuts_removed: list[Path] = field(default_factory=list)
    login_forgotten: bool = False
    data_dir_removed: Path | None = None
    logs_removed: Path | None = None
    warnings: list[str] = field(default_factory=list)
    trees_removed: list[Path] = field(default_factory=list)
    #: True when a requested deletion was refused/failed and the profile was kept.
    profile_kept: bool = False
    #: True when Remove stopped before deleting or forgetting anything (the game runs, another
    #: Nadare command holds it, the settings cannot be saved). Not set for a partial deletion.
    refused: bool = False


@dataclass
class RemovableTree:
    key: str
    path: Path
    safe: bool
    reason: str


def _nadare_dirs() -> list[Path]:
    out: list[Path] = []
    for fn in (paths.data_dir, paths.config_dir, paths.state_dir, paths.cache_dir):
        try:
            out.append(fn().resolve())
        except OSError:
            continue
    return out


def _setting(profile, key: str):
    """A profile key from the top level or its ``extra`` mapping (like the backends)."""
    value = profile.get(key)
    if value not in (None, ""):
        return value
    extra = profile.get("extra")
    if isinstance(extra, dict) and extra.get(key) not in (None, ""):
        return extra[key]
    return None


def _shared_with(config_profiles, name: str, resolved: Path) -> str:
    """Name of another game whose folders overlap ``resolved`` ("" if none).

    The one answer to "used by another game" (:func:`location.overlapping`, #1372 step 2):
    recorded paths, ``owned_dirs``, its game folder and its ``profiles/<name>``. Every recorded
    folder counts here, broad or not, so deletion only ever refuses more.
    """
    claim = location.overlapping(
        config_profiles, resolved, excluding=name, direction="either", ignore_broad=False
    )
    return claim.owner if claim is not None else ""


def _own_folder(name: str) -> Path:
    """``profiles/<name>`` with only the profiles folder resolved, never ``<name>`` itself.

    Resolving the game's own folder would follow a link put there (to Documents, say) and
    call the link's target Nadare's (storage review B1). Resolving the profiles folder keeps
    systems where ``/home`` is a link (Silverblue, Bazzite) working.
    """
    return paths.profiles_dir().resolve() / name


def _adopted_game_folder(config_profiles, name: str) -> Path | None:
    """``profiles/Nadare/<Game>`` when that is this game's recorded folder, else ``None``.

    An older Move put a game folder there; Find games adopts it with ``owned_dirs`` (#1366
    C-F2). Only that exact shape counts: the folder :func:`location.game_folder` names, directly
    in ``profiles/Nadare`` and not a link, so nothing else inside Nadare's folders opens up.
    """
    profile = config_profiles.get(name) if isinstance(config_profiles, Mapping) else None
    if not isinstance(profile, dict):
        return None
    try:
        folder = location.game_folder(profile, name)
        if folder is None or folder.is_symlink():
            return None
        real = folder.parent.resolve() / folder.name
        if real.parent != paths.profiles_dir().resolve() / location.NADARE_DIR:
            return None
    except (OSError, RuntimeError):
        return None
    return real


def tree_safety(config_profiles, name: str, path) -> tuple[bool, str]:
    """Whether ``path`` may be deleted on behalf of profile ``name``, and why not.

    Refuses symlinks, mount points, home or its ancestors, Nadare's own
    directories (other than this profile's own folder) and trees overlapping
    another profile's configured install dir / prefix.
    """
    expanded = Path(path).expanduser()
    if expanded.is_symlink():
        return False, "is a symlink"
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = expanded
    home = Path.home().resolve()
    if os.path.ismount(resolved):
        return False, "is a mount point"
    if resolved == home or resolved in home.parents or resolved == Path("/"):
        return False, "is home or an ancestor of home"
    try:
        own = _own_folder(name)
    except OSError:
        own = None
    adopted = _adopted_game_folder(config_profiles, name)
    for nd in _nadare_dirs():
        if resolved == nd or resolved in nd.parents:
            return False, "is inside nadare's own directories"
        if nd in resolved.parents:
            # Only this profile's own folder (and below) is deletable.
            if own is not None and (resolved == own or own in resolved.parents):
                continue
            if adopted is not None and (resolved == adopted or adopted in resolved.parents):
                continue
            return False, "is inside nadare's own directories"
    other = _shared_with(config_profiles, name, expanded)
    if other:
        return False, f"shared with {other}"
    return True, ""


def _parse_mountinfo(text: str) -> frozenset[str]:
    """Mount points listed in ``/proc/self/mountinfo`` text (field 5, octal escapes undone)."""
    points: set[str] = set()
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    return frozenset(points)


def _mountinfo_points() -> frozenset[str] | None:
    """Every mount point the kernel lists, including same-filesystem bind mounts.

    ``None`` when the list cannot be read: an unknown inventory is not an empty one (#1483)."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return _parse_mountinfo(handle.read())
    except OSError:
        return None


class _Mounts(NamedTuple):
    """What a deletion needs to tell a mount from a folder: root's device and the mount list."""

    dev: int
    points: frozenset[str]


class _RmTreeResult(NamedTuple):
    """Mount points left alone, and folders that could not be removed with the reason."""

    mounts: list[Path]
    leftovers: list[tuple[Path, OSError]]


def _is_mount(path: Path, mounts: _Mounts) -> bool:
    """Whether ``path`` is a mount boundary: a mount point, a bind mount listed in
    ``mounts.points``, or a folder on another device than ``mounts.dev``."""
    try:
        return (
            os.path.ismount(path)
            or path.stat().st_dev != mounts.dev
            or os.path.realpath(path) in mounts.points
        )
    except OSError:
        return True  # cannot be inspected: kept like a mount, never descended into (#1483)


def _has_mount_below(root: Path, mounts: _Mounts) -> bool:
    """Whether any folder below ``root`` is a separate mounted drive (symlinks not followed).

    A game can plant a tree deeper than Python's recursion limit; the walk keeps its
    own stack so such a tree is scanned rather than crashing the Clear.
    """
    pending = [root]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            if _is_mount(path, mounts):
                return True
            pending.append(path)
    return False


def _explained_rmdir_failure(
    path: Path,
    keep: list[Path],
    kept: list[Path],
    failed: Sequence[tuple[Path, OSError]] = (),
) -> bool:
    """Whether a folder left behind holds a mount, a ``keep`` tree or an already reported
    stuck folder, so it could not empty (only the deepest failure is worth a warning)."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if any(resolved == k or resolved in k.parents for k in keep):
        return True
    if any(resolved == m or resolved in m.parents for m in kept):
        return True
    return any(path in stuck.parents for stuck, _exc in failed)


def _leftover_warning(path: Path, exc: OSError) -> str:
    """A plain-language warning for a folder that is still there after a deletion."""
    return f"{path} could not be removed ({exc.strerror or exc}); remove it by hand"


def _rmtree_contents(
    root: Path,
    mounts: _Mounts,
    keep: list[Path],
    kept: list[Path],
    failed: list[tuple[Path, OSError]],
) -> None:
    """Delete ``root``'s contents, leaving mounts and the ``keep`` trees in place.

    The walk keeps its own stack (a deep game-planted tree must not hit the recursion
    limit) and removes a folder after everything inside it was handled.
    """
    pending: list[tuple[Path, bool]] = [(root, False)]
    while pending:
        folder, finished = pending.pop()
        if finished:
            try:
                folder.rmdir()
            except OSError as exc:
                # A mount or a kept tree inside it explains the failure; anything else is
                # a folder the user has to remove by hand.
                if not _explained_rmdir_failure(folder, keep, kept, failed):
                    failed.append((folder, exc))
            continue
        for entry in folder.iterdir():
            if entry.is_symlink():
                entry.unlink()
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                resolved = entry
            if any(resolved == k or k in resolved.parents for k in keep):
                continue  # the kept tree itself or something inside it
            if entry.is_dir():
                if _is_mount(entry, mounts):
                    kept.append(entry)
                    continue
                pending.append((entry, True))
                pending.append((entry, False))
            else:
                entry.unlink()


def _rmtree_safely(root: Path, keep: Iterable[Path] = ()) -> _RmTreeResult:
    """Delete ``root`` but never descend into a mounted drive or a ``keep`` tree.

    Any folder that is a mount point, lives on another device than ``root``, or is
    listed in ``/proc/self/mountinfo`` (a same-disk bind mount, which neither
    ``os.path.ismount`` nor ``st_dev`` can see), is left untouched and returned so
    the caller can report it. ``keep`` trees, and the folders leading to them, are
    left in place as before. A folder that could not be removed for any other
    reason is returned as a leftover with the original error. Mount points are
    returned outermost first.
    """
    kept: list[Path] = []
    failed: list[tuple[Path, OSError]] = []
    if root.is_symlink():
        # The walk below would follow it and empty whatever the link points at; a plain
        # ``shutil.rmtree`` refuses a link too. The caller reports it as kept.
        return _RmTreeResult(
            [], [(root, OSError("the folder is a link, so nothing behind it was deleted"))]
        )
    keep = [k.resolve() for k in keep]
    try:
        root_dev = root.stat().st_dev
    except OSError:
        root_dev = -1
    points = _mountinfo_points()
    if points is None:
        # Without the kernel's mount list a same-disk bind mount is invisible: delete nothing
        # rather than cross one (#1483). The caller reports the folder as kept.
        return _RmTreeResult(
            [], [(root, OSError("could not read the mount list (/proc/self/mountinfo); "
                                "nothing was deleted"))]
        )
    mounts = _Mounts(root_dev, points)
    if _is_mount(root, mounts):
        return _RmTreeResult([root], failed)
    if not keep and not _has_mount_below(root, mounts):
        shutil.rmtree(root)
        return _RmTreeResult(kept, failed)
    _rmtree_contents(root, mounts, keep, kept, failed)
    if not keep and not kept:
        try:
            root.rmdir()  # everything inside went, so the (now empty) root can too
        except OSError as exc:
            if not _explained_rmdir_failure(root, keep, kept, failed):
                failed.append((root, exc))
    return _RmTreeResult(kept, failed)


def delete_tree(root: Path, keep: Iterable[Path] = ()) -> _RmTreeResult:
    """The one way to delete a folder tree on the player's behalf (#1481): never crosses a
    mounted drive or bind mount, never deletes without a readable mount list. Everything that
    stayed is in the result; a caller reports it rather than claiming the folder is gone."""
    return _rmtree_safely(Path(root), keep)


def _is_mount_fd(fd: int, parent_fd: int, mounts: _Mounts) -> bool:
    """Whether an open folder is a mount boundary (the rules of :func:`_is_mount`)."""
    try:
        if os.fstat(fd).st_dev != os.fstat(parent_fd).st_dev:
            return True
        real = os.path.realpath(f"/proc/self/fd/{fd}")
    except OSError:
        return True  # cannot be inspected: kept like a mount, never descended into
    return real in mounts.points


def _remove_fd_tree(
    root: Path, root_fd: int, mounts: _Mounts
) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    """Delete every entry below an open folder, opening each name afresh.

    Names are opened with ``O_NOFOLLOW`` relative to the folder that listed them, so a
    hostile process swapping a child for a link between the listing and the visit makes
    the entry a leftover instead of redirecting the deletion outside the tree.
    """
    kept: list[Path] = []
    failed: list[tuple[Path, OSError]] = []
    pending: list[tuple[str, int, str | None, Path]] = [("scan", root_fd, None, root)]
    while pending:
        action, fd, name, path = pending.pop()
        if action == "close":
            os.close(fd)
            continue
        if action == "rmdir":
            assert name is not None
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError as exc:
                if not _explained_rmdir_failure(path, [], kept, failed):
                    failed.append((path, exc))
            continue
        try:
            entries = list(os.scandir(fd))
        except OSError as exc:
            failed.append((path, exc))
            continue
        for entry in entries:
            child = path / entry.name
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                failed.append((child, exc))
                continue
            if not stat.S_ISDIR(mode):
                try:
                    os.unlink(entry.name, dir_fd=fd)
                except OSError as exc:
                    failed.append((child, exc))
                continue
            try:
                child_fd = os.open(
                    entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except OSError as exc:
                failed.append((child, exc))
                continue
            if _is_mount_fd(child_fd, fd, mounts):
                kept.append(child)
                os.close(child_fd)
                continue
            pending.append(("close", child_fd, None, child))
            pending.append(("rmdir", fd, entry.name, child))
            pending.append(("scan", child_fd, None, child))
    return kept, failed


class PinnedTree:
    """A folder opened by descriptor, so measuring and deleting cannot be redirected."""

    def __init__(self, root: Path, parent_fd: int, fd: int) -> None:
        self.root = root
        self._parent_fd = parent_fd
        self._fd = fd

    @property
    def handle(self) -> Path:
        """A stable path to the opened folder, safe to measure after a path swap."""
        return Path(f"/proc/self/fd/{self._fd}")

    def delete(self) -> _RmTreeResult:
        """Delete the opened folder's contents and then the folder itself."""
        points = _mountinfo_points()
        if points is None:
            return _RmTreeResult(
                [],
                [
                    (
                        self.root,
                        OSError(
                            "could not read the mount list (/proc/self/mountinfo); "
                            "nothing was deleted"
                        ),
                    )
                ],
            )
        mounts = _Mounts(os.fstat(self._fd).st_dev, points)
        if _is_mount_fd(self._fd, self._parent_fd, mounts):
            return _RmTreeResult([self.root], [])
        kept, failed = _remove_fd_tree(self.root, self._fd, mounts)
        try:
            os.rmdir(self.root.name, dir_fd=self._parent_fd)
        except OSError as exc:
            if not _explained_rmdir_failure(self.root, [], kept, failed):
                failed.append((self.root, exc))
        return _RmTreeResult(kept, failed)


@contextlib.contextmanager
def pinned_tree(root: Path) -> Iterator[PinnedTree]:
    """Open ``root`` itself and its parent, refusing a link in the final name.

    Holding both descriptors means a later rename or symlink swap of that path cannot
    redirect the folder's measurement or deletion to another one.
    """
    root = Path(root)
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(
            root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError:
        os.close(parent_fd)
        raise
    try:
        yield PinnedTree(root, parent_fd, fd)
    finally:
        os.close(fd)
        os.close(parent_fd)


def _not_owned_reason(profile, key: str, path: Path, name: str) -> str:
    """Why ``path`` has no ownership record, or "" when Nadare made it.

    Empty for the profile's own folder under Nadare's data dir, for a key the Add game dialog
    recorded in ``owned_trees`` (the folder was new or empty when the game was added). The
    record also holds the folder's path (``owned_paths``); it only counts while the profile
    still points at that same folder, so a path changed later is not covered by it. A folder
    the user picked, such as a games library or a shared ``~/.wine``, is never offered for
    deletion, even when it looks like a Wine prefix.
    """
    try:
        own = _own_folder(name)
        resolved = path.resolve()
    except OSError:
        return _NOT_CREATED
    if key in ("prefix", "wineprefix") and not _setting(profile, "prefix") and not _setting(
        profile, "wineprefix"
    ):
        # The backend's default prefix lives in this profile's own folder: Nadare made it.
        default = paths.profiles_dir() / name / "prefix"
        try:
            if not default.is_symlink() and resolved == _own_folder(name) / "prefix":
                return ""
        except OSError:
            pass
    owned = profile.get("owned_trees")
    if isinstance(owned, list) and key in owned:
        recorded = profile.get("owned_paths")
        if not isinstance(recorded, dict) or key not in recorded:
            # A profile made before paths were recorded (0.1.0b13): only a path that reaches
            # the folder without any link can still be the one Nadare made. A game folder
            # swapped for a link to e.g. Documents must never be emptied (chaos FS-1).
            literal = Path(os.path.abspath(os.path.expanduser(str(path))))
            if literal != resolved:
                return (
                    "Nadare made this folder, but the path to it now goes through a link, "
                    "so it is kept (remove it yourself if you want it gone)"
                )
            return ""
        # The record was resolved when Nadare made the folder. Compare it AS RECORDED:
        # resolving it again would follow a link put in its place later, and both sides would
        # then name the link's target (chaos FS-1: a game folder swapped for a link to
        # Documents had Documents/install deleted).
        same = Path(os.path.normpath(str(recorded[key]))) == resolved
        if same:
            return ""
        return (
            "Nadare made this folder, but its location changed since then, so it is "
            "kept (remove it yourself if you want it gone)"
        )
    if resolved == own or own in resolved.parents:
        return ""
    return _NOT_CREATED


_NOT_CREATED = (
    "Nadare did not create this folder, so it will not delete it "
    "(remove it yourself if you want it gone)"
)


def prefix_made_by_nadare(profile, path: Path, name: str) -> bool:
    """Whether ``path``, the profile's Wine prefix, is one Nadare made (Move may delete it)."""
    try:
        return _created_by_nadare(profile, "prefix", Path(path), name)
    except OSError:
        return False


def _created_by_nadare(profile, key: str, path: Path, name: str) -> bool:
    """Whether Nadare made ``path``, so deleting it cannot take anything of the user's."""
    return not _not_owned_reason(profile, key, path, name)


def _configured_tree_path(profile, key: str, name: str) -> Path | None:
    """The path a tree key points at, whether or not it still exists."""
    value = _setting(profile, key)
    if isinstance(value, str) and value:
        return Path(value).expanduser()
    if key == "prefix":
        return default_prefix(name)
    return None


def _same_tree_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        left = Path(os.path.abspath(os.path.expanduser(str(a))))
        right = Path(os.path.abspath(os.path.expanduser(str(b))))
        return os.path.normpath(str(left)) == os.path.normpath(str(right))


def _pending_removed_path(profile, key: str, name: str) -> Path | None:
    """A missing tree this profile recorded as being removed before a crash, if still ours."""
    pending = profile.get(_PENDING_REMOVAL)
    if not isinstance(pending, dict):
        return None
    recorded = pending.get(key)
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = _configured_tree_path(profile, key, name)
    if candidate is None and key == "prefix" and not (
        _setting(profile, "prefix") or _setting(profile, "wineprefix")
    ):
        # The default prefix is only "configured" while it exists, so the one a crash left
        # half-removed must be named here, or the uninstall never finishes (review B4).
        candidate = paths.profiles_dir() / name / "prefix"
    if candidate is None or not _same_tree_path(candidate, Path(recorded)):
        return None
    if _not_owned_reason(profile, key, candidate, name):
        return None  # only a tree Nadare owns may be treated as already removed
    return Path(recorded)


def _unrecorded_rebuild_backups(profile, name: str) -> list[Path]:
    """``<prefix>.broken-*`` folders next to the game's prefix that it did not record."""
    if not isinstance(profile, dict):
        return []
    prefixes = [paths.profiles_dir() / name / "prefix"]
    for key in ("prefix", "wineprefix"):
        value = profile.get(key)
        if isinstance(value, str) and value:
            prefixes.append(Path(value).expanduser())
    recorded = set()
    for value in _recorded_backups(profile):
        try:
            recorded.add(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    found: list[Path] = []
    for prefix in dict.fromkeys(prefixes):
        if not prefix.is_absolute():
            continue
        try:
            entries = list(prefix.parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(f"{prefix.name}.broken-"):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                if entry.resolve() in recorded:
                    continue
            except (OSError, RuntimeError):
                pass
            if entry not in found:
                found.append(entry)
    return found


def _recorded_backups(profile) -> list[str]:
    """The unique backup paths a profile recorded after rebuilding its prefix."""
    values = profile.get("prefix_backups")
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def _backup_reason(profile, path: Path) -> str:
    """Why ``path`` is not a rebuild backup that is safe to delete ("" when it is).

    It must be a real folder named the way a rebuild names its backups, and it must not be, hold,
    or lie inside any folder the profile uses right now (a prefix rolled back by linking to the
    backup, for one, makes the backup the live prefix).
    """
    if not path.is_dir():
        return "is not a folder"
    if not _BACKUP_NAME.match(path.name):
        return _BACKUP_NOT_NAMED
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return "could not be resolved"
    for key in _TREE_KEYS:
        value = _setting(profile, key)
        if not isinstance(value, str) or not value:
            continue
        try:
            live = Path(value).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if live == real or real in live.parents or live in real.parents:
            return _BACKUP_IS_LIVE
    return ""


def default_prefix(name: str) -> Path | None:
    """The prefix a Wine-family profile that sets none uses: ``<profiles_dir>/<name>/prefix``."""
    path = paths.profiles_dir() / name / "prefix"
    if path.is_dir() and not path.is_symlink():
        return path
    return None


def _outside_game_folder(config, name: str, path: Path) -> str:
    """Why a catalog game's folder lies outside its own game folder, or "" (#1229).

    Only ever narrows what may be deleted: on top of the ownership records, a catalog game's
    deletable folders must sit inside the game folder of the Steam-like model. Custom Wine
    games, scripts and Steam games are outside that model and keep the ownership rule alone.
    """
    from . import gamefolders

    dirs = (config.profiles.get(name) or {}).get("owned_dirs")
    if not isinstance(dirs, list) or not dirs:
        # Older profiles never recorded their game folder (a launcher game's prefix may be the
        # game folder itself): only a recorded one is strong enough to narrow by.
        return ""
    try:
        place = gamefolders.placement(config, name)
    except Exception:  # noqa: BLE001 - an unknown layout means "keep it"
        return "its game folder could not be worked out, so it is kept"
    if place is None or place.folder is None or not place.made_by_nadare:
        return ""
    try:
        resolved = path.resolve()
        folder = place.folder.resolve()
    except OSError:
        return "it could not be resolved, so it is kept"
    if resolved == folder or folder in resolved.parents:
        return ""
    from . import relink  # removal is imported by relink's helpers: import late

    if relink.moved_by_nadare(config.profiles.get(name) or {}, path):
        return ""  # Nadare moved it there itself (path and folder identity match, #1366 A3)
    return (
        "it is outside the game's own folder, so it is kept (remove it yourself if you want "
        "it gone)"
    )


def removable_trees(config, name: str) -> list[RemovableTree]:
    """List the install/prefix trees configured for ``name`` and whether each is safe to delete."""
    profile = config.profiles.get(name, {})
    found: list[RemovableTree] = []
    for key in _TREE_KEYS:
        value = _setting(profile, key)
        if not value or not isinstance(value, str):
            if key != "prefix":
                continue
            expanded = default_prefix(name)
            if expanded is None:
                continue
        else:
            expanded = Path(value).expanduser()
            try:
                present = expanded.is_dir()
            except OSError:
                # An over-long or otherwise unusable configured path is not a tree we can
                # delete; the missing-tree warning below keeps the profile instead of crashing.
                present = False
            if not present:
                continue
        safe, reason = tree_safety(config.profiles, name, expanded)
        if safe:
            # The default prefix is inside this profile's own folder, so ``_not_owned_reason``
            # already counts it as Nadare's.
            reason = _not_owned_reason(profile, key, expanded, name)
            if reason:
                safe = False
        if safe:
            reason = _outside_game_folder(config, name, expanded)
            if reason:
                safe = False
        if expanded.is_symlink():
            shown = expanded
        else:
            try:
                shown = expanded.resolve()
            except OSError:
                shown = expanded
        found.append(RemovableTree(key=key, path=shown, safe=safe, reason=reason))
    backup_keys: set[str] = set()
    for value in _recorded_backups(profile):
        expanded = Path(value).expanduser()
        if not expanded.exists():
            continue
        safe, reason = tree_safety(config.profiles, name, expanded)
        if safe:
            reason = _backup_reason(profile, expanded)
            if reason:
                safe = False
        if expanded.is_symlink():
            shown = expanded
        else:
            try:
                shown = expanded.resolve()
            except OSError:
                shown = expanded
        # Keyed by folder name, not by position: a ticked backup stays the one that is deleted
        # even if an earlier one disappears between the dialog and the deletion.
        key = f"{_BACKUP_KEY}:{expanded.name}"
        while key in backup_keys:
            key += "'"
        backup_keys.add(key)
        found.append(RemovableTree(key=key, path=shown, safe=safe, reason=reason))
    return found


def _drop_lone_manifest(folder: Path) -> None:
    """Delete the library manifest when it is all that is left in the game's folder.

    With the game's files gone the manifest describes nothing. When files were kept it stays,
    so adding the library back finds the game again; a file the user put there keeps it too.
    """
    ours = {libraries.MANIFEST_NAME, libraries.MANIFEST_TMP_NAME}
    try:
        entries = list(folder.iterdir())
        # An interrupted write leaves the temp file next to it (#1362): Nadare's too.
        if entries and all(
            e.name in ours and e.is_file() and not e.is_symlink() for e in entries
        ):
            for entry in entries:
                entry.unlink()
    except OSError:
        pass


def _default_library_folder(prefix: Path, install: Path, name: str | None) -> Path | None:
    """This profile's own ``profiles/<name>`` when its prefix or install folder sits directly
    inside it; never another profile's folder (storage review round 3, C-F3)."""
    try:
        root = paths.profiles_dir().resolve()
    except OSError:
        return None
    for tree in (prefix, install):
        if not str(tree) or not tree.is_absolute() or tree.name not in ("prefix", "install"):
            continue
        home = tree.parent
        try:
            if home.is_symlink() or home.parent.resolve() != root:
                continue
        except OSError:
            continue
        if name is None or home.name != name:
            continue
        return home
    return None


def _claimed_by_another(folder: Path, name: str, others: Mapping[str, Any]) -> bool:
    """Whether another profile's folder is ``folder`` (by its name in the default library, or
    by its own answer): never tidied for this one (C-F3, review of #1374)."""
    try:
        real = folder.resolve()
        in_default = folder.parent.resolve() == paths.profiles_dir().resolve()
    except OSError:
        return True
    for other, data in others.items():
        if other == name or not isinstance(data, Mapping):
            continue
        if in_default and folder.name == other:
            return True
        theirs = location.game_folder(data, str(other))
        try:
            if theirs is not None and theirs.resolve() == real:
                return True
        except OSError:
            return True
    return False


def _wrapper_folders(
    profile: dict, name: str | None = None, others: Mapping[str, Any] | None = None
) -> list[str]:
    """The per-game folders Nadare made around the game: ``owned_dirs``, else the Add Game shape.

    A profile older Nadare made recorded no ``owned_dirs``; its owned ``prefix`` and ``install``
    folders still sit in ``<library>/Nadare/<Game>``, which is tidied the same way once empty.
    """
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list) and dirs:
        # Only the game's own wrapper counts (#622): a folder Nadare recorded for this game
        # (install folder, prefix, their recorded paths, prefix backups) or the folder directly
        # around one, as Add Game lays it out. A hand-edited or damaged entry pointing anywhere
        # else is ignored, so removal never rmdirs an unrelated empty folder.
        recorded: list[object] = [_setting(profile, "install_dir"), _setting(profile, "prefix")]
        paths_map = profile.get("owned_paths")
        if isinstance(paths_map, dict):
            recorded += list(paths_map.values())
        backups = profile.get("prefix_backups")
        if isinstance(backups, list):
            recorded += backups
        allowed: set[Path] = set()
        for value in recorded:
            if value:
                own = Path(str(value)).expanduser()
                if own.is_absolute():
                    allowed.update((own, own.parent))
        kept = []
        for d in dirs:
            entry = Path(str(d)).expanduser()
            if entry.is_absolute() and entry in allowed:
                kept.append(str(d))
        return kept
    if name is not None and location.catalog_kind(profile.get("backend")):
        # The one answer to "which folder is this game's own" (#1372); nothing else for a
        # catalog game, so Settings and Uninstall never disagree about it.
        folder = location.game_folder(profile, name)
        if folder is None:
            # Not the game's own folder (the player's prefix is elsewhere, s21), but Nadare's
            # install may still sit in profiles/<name>: tidy that once it is empty, so no stale
            # manifest is left for Find games (#1366). Step 5c only rmdirs an empty folder.
            folder = _default_library_folder(
                *(
                    Path(str(_setting(profile, k) or "")).expanduser()
                    for k in ("prefix", "install_dir")
                ),
                name,
            )
        if folder is None or _claimed_by_another(folder, name, others or {}):
            return []
        return [str(folder)]
    owned = profile.get("owned_trees")
    prefix, install = (
        Path(str(_setting(profile, k) or "")).expanduser() for k in ("prefix", "install_dir")
    )
    own = _default_library_folder(prefix, install, name)
    if own is not None:
        # The game's own profiles/<name> (#1362): step 5c only rmdirs it once it is empty,
        # after dropping a lone manifest, so a kept data file keeps it.
        return [str(own)]
    if not isinstance(owned, list) or "prefix" not in owned or prefix.name != "prefix":
        return []
    home = prefix.parent
    if home.parent.name != "Nadare":
        return []
    if _setting(profile, "install_dir") and (
        install != home / "install" or "install_dir" not in owned
    ):
        return []  # the game folder is somewhere else, so this is not the Add Game shape
    return [str(home)]


def remove_game(
    config,
    name: str,
    *,
    delete_data: bool = False,
    delete_trees: Iterable[str] = (),
    delete_backups: bool = False,
    applications_dir=None,
    desktop_dir=None,
    save: bool = True,
    drop_profile: bool = True,
) -> RemovalResult:
    """Remove ``name`` from ``config`` and clean up its shortcuts/login/data.

    ``delete_backups`` also deletes every recorded rebuild backup that passes the same
    checks as the Settings button. ``drop_profile=False`` skips the config mutation so
    the GUI can run the slow parts (keyring, deleting a prefix) on a worker thread and
    drop the profile itself on the GUI thread.
    """
    if name not in config.profiles:
        raise backends.BackendError(f"no such profile: {name!r}")

    lock = process.OperationLock(name)
    try:
        lock.acquire()
    except process.ProfileLockError as exc:
        if exc.unusable:
            raise backends.BackendError(str(exc)) from None
        raise backends.BackendError(process.OPERATION_BUSY.format(name=name)) from None
    # This separate profile lock stops another Nadare command (such as Move)
    # from overlapping the deletion; it does not stop a game from launching, so
    # ``_running_problem`` is checked again right before the first deletion.
    game_lock = process.ProfileLock(name)
    try:
        try:
            game_lock.acquire()
        except process.ProfileLockError as exc:
            if exc.unusable:
                raise backends.BackendError(str(exc)) from None
            result = RemovalResult(profile=name, profile_kept=True, refused=True)
            result.warnings.append(
                "this game's profile is in use by another Nadare command; nothing was deleted"
            )
            return result
        problem = _running_problem(config, name)
        if problem is not None:
            result = RemovalResult(profile=name, profile_kept=True, refused=True)
            result.warnings.append(problem)
            return result
        return _remove_locked(
            config,
            name,
            delete_data=delete_data,
            delete_trees=delete_trees,
            delete_backups=delete_backups,
            applications_dir=applications_dir,
            desktop_dir=desktop_dir,
            save=save,
            drop_profile=drop_profile,
        )
    finally:
        game_lock.release()
        lock.release()


def _running_problem(config, name: str) -> str | None:
    """Why the game may not be removed right now, or None when it is safe.

    The same status probe Move uses. A probe that cannot answer refuses too:
    deleting a prefix under a possibly running game is not recoverable.
    """
    profile = config.profiles.get(name) or {}
    if backends.retired_replacement(profile.get("backend")):
        # A retired kind has no backend to ask (``create`` raises), but a b22 user may still
        # have its game open: look for anything running in its prefix instead.
        return _retired_running_problem(profile, name)
    try:
        backend = backends.create(name, config)
    except Exception:  # noqa: BLE001 - this build cannot load it; the game may still run (#1493)
        # Another Nadare (another build, another Flatpak window) may have started it: ask the
        # prefix itself, as for a retired kind. Proved idle, the profile can still go.
        return _retired_running_problem(profile, name)
    return _running_problem_for(backend)


def _running_problem_for(backend: Any) -> str | None:
    try:
        status = backend.status()
    except Exception as exc:  # noqa: BLE001 - unknown means do not delete
        return f"could not check whether the game is running ({exc}); nothing was deleted"
    if gamefacts.is_running(status):
        return "the game is running (stop it first); nothing was deleted"
    return None


def _retired_running_problem(profile: dict, name: str) -> str | None:
    """Whether anything still runs in the game's prefix, asked without its backend (a retired
    kind, or one this build cannot load): its Wine processes or a run lock held by another
    Nadare. A scan that cannot answer refuses too."""
    raw = str(_setting(profile, "prefix") or _setting(profile, "wineprefix") or "").strip()
    prefix = Path(raw).expanduser() if raw else default_prefix(name)
    if prefix is None:
        return None
    try:
        busy = bool(process.prefix_pids(prefix))
        busy = busy or runlock.running(prefix)
    except Exception as exc:  # noqa: BLE001 - unknown means do not delete
        return f"could not check whether the game is running ({exc}); nothing was deleted"
    if busy:
        return (
            "the game or its launcher is still running (close it first); nothing was deleted"
        )
    return None


def _remove_locked(
    config,
    name: str,
    *,
    delete_data: bool,
    delete_trees: Iterable[str],
    delete_backups: bool,
    applications_dir,
    desktop_dir,
    save: bool,
    drop_profile: bool,
) -> RemovalResult:
    result = RemovalResult(profile=name)
    # The game's wrapper folders, worked out before anything is deleted: the steps below drop
    # recorded backups (and maybe the profile), which would hide what the wrapper held.
    wrappers = _wrapper_folders(
        copy.deepcopy(config.profiles.get(name, {})), name, others=config.profiles
    )
    # Old-prefix backups no version recorded (a rebuild killed before #1490): never chosen
    # in the dialog, so "Nadare's data" keeps them rather than deleting them unseen.
    unrecorded = _unrecorded_rebuild_backups(config.profiles.get(name, {}), name)

    # 1. Check every requested tree first. A missing or unsafe one is skipped
    # with a warning (and the profile is kept), but it must not cancel the safe
    # trees the user also ticked.
    requested = list(dict.fromkeys(delete_trees))
    trees: list[RemovableTree] = []
    keep_profile = False
    done: list[Path] = []
    if requested:
        by_key = {t.key: t for t in removable_trees(config, name)}
        trees = [by_key[k] for k in requested if k in by_key and by_key[k].safe]
        alias_requested = {k for k in requested if k in ("prefix", "wineprefix")}
        alias_found = any(k in by_key for k in alias_requested)
        missing: list[str] = []
        for key in requested:
            if key in by_key:
                continue
            if key in ("prefix", "wineprefix") and (
                alias_found or any(k in missing for k in alias_requested)
            ):
                continue  # prefix/wineprefix are two names for the same tree
            removed = _pending_removed_path(config.profiles.get(name, {}), key, name)
            if removed is not None:
                done.append(removed)  # a killed Remove already deleted this owned tree
                continue
            missing.append(key)
        labels = {
            "install_dir": "install folder",
            "prefix": "Wine prefix",
            "wineprefix": "Wine prefix",
        }
        for key in missing:
            result.warnings.append(
                f"no configured {labels.get(key, key)} was found for {name}; it was kept"
            )
            keep_profile = True
        for tree in (by_key[k] for k in requested if k in by_key):
            if not tree.safe:
                result.warnings.append(f"not deleting {tree.key}: {tree.reason}")
                keep_profile = True

    # 2. Before dropping the profile, prove the config can be persisted. Otherwise a
    # read-only config directory would leave the files gone but the profile on
    # disk. A no-op save is the cheapest such preflight.
    if drop_profile and save and not keep_profile:
        profile = config.profiles[name]
        pending = {
            key: str(by_key[key].path)
            for key in requested
            if key in by_key and by_key[key].safe
        }
        if pending:
            profile[_PENDING_REMOVAL] = pending
        else:
            profile.pop(_PENDING_REMOVAL, None)
        try:
            config.save()  # also records the pending deletion before the first one starts
        except OSError as exc:
            profile.pop(_PENDING_REMOVAL, None)
            result.warnings.append(f"could not save config: {exc}")
            result.profile_kept = True
            result.refused = True
            return result

    # 3. The game could have started since the probe in ``remove_game``; a launch
    # takes no lock of ours. Check again right before the first irreversible
    # step, and on any doubt keep the profile and change nothing.
    problem = _running_problem(config, name)
    if problem is not None:
        result.warnings.append(problem)
        result.profile_kept = True
        result.refused = True
        return result

    # 4. Opt-in deletion of the configured install dir / prefix trees. These go
    # first: only when every requested tree was handled without a skip or a
    # failure may the shortcuts, login and data folder follow.
    touched = False  # any deletion started (a partly deleted tree is not "nothing changed")
    # Trees of this profile the user did not tick: never delete one nested inside a ticked
    # one. Only those inside each tree are passed on, so an unrelated unticked tree does
    # not stop the ticked tree's own (now empty) root from going.
    all_trees = removable_trees(config, name) if requested else []
    chosen_keys = set(requested)
    if chosen_keys & {"prefix", "wineprefix"}:
        chosen_keys |= {"prefix", "wineprefix"}  # two names for the same tree
    unticked = [t.path for t in all_trees if t.key not in chosen_keys]
    # Outermost first; a tree already gone with its parent is not an error.
    for tree in sorted(trees, key=lambda t: len(t.path.parts)):
        if any(d == tree.path or d in tree.path.parents for d in done):
            continue
        try:
            tree_resolved = tree.path.resolve()
        except OSError:
            tree_resolved = tree.path
        inner_keep: list[Path] = []
        for path in unticked:
            try:
                if path.resolve().is_relative_to(tree_resolved):
                    inner_keep.append(path)
            except OSError:
                continue
        touched = True
        try:
            outcome = _rmtree_safely(tree.path, keep=inner_keep)
            for mount in outcome.mounts:
                result.warnings.append(f"{mount} is a separate mounted drive, left alone")
            if outcome.mounts:
                keep_profile = True  # the tree was only partly removed
            for path, exc in outcome.leftovers:
                result.warnings.append(_leftover_warning(path, exc))
                keep_profile = True
            for kept in inner_keep:
                if kept.exists() or kept.is_symlink():
                    result.warnings.append(
                        f"kept {kept} because it was not selected for deletion"
                    )
                    keep_profile = True
            if not tree.path.exists():
                result.trees_removed.append(tree.path)
                done.append(tree.path)
        except FileNotFoundError:
            done.append(tree.path)
        except OSError as exc:
            result.warnings.append(f"could not remove {tree.key}: {exc}")
            keep_profile = True

    # 4b. Old rebuild backups, when asked. Settings deletes them through the same helper,
    # which re-checks naming, ownership and liveness; a skipped backup is only reported and
    # does not keep the profile, since it is an old leftover rather than a configured tree.
    if delete_backups:
        from . import prefixmove

        for tree in removable_trees(config, name):
            if not tree.key.startswith(_BACKUP_KEY):
                continue
            if not tree.safe:
                result.warnings.append(f"not deleting backup {tree.path}: it {tree.reason}")
                continue
            try:
                prefixmove.delete_backup(config, name, tree.path)
            except (prefixmove.PrefixMoveError, OSError) as exc:
                result.warnings.append(f"could not delete backup {tree.path}: {exc}")
                continue
            result.trees_removed.append(tree.path)
            done.append(tree.path)

    # 5. Desktop/menu shortcuts owned by this profile, the saved login and the
    # profile data folder. When the profile is kept because a tree was skipped or
    # failed, leave these alone too: they still belong to it.
    if not keep_profile:
        result.shortcuts_removed = shortcuts.remove_desktop_entries(
            name, applications_dir=applications_dir, desktop_dir=desktop_dir
        )

        steam_files, steam_warnings = shortcuts.remove_steam_shortcuts(name)
        result.shortcuts_removed += steam_files
        result.warnings += steam_warnings

        # 5a. Saved login, if the backend supports remembering one.
        kind = config.profiles[name].get("backend", "")
        try:
            cls = backends.get(kind)
        except backends.BackendError:
            cls = None
        if cls is not None and getattr(cls, "supports_remember_me", False):
            try:
                cls.forget_saved_login(name)
                # A login held in memory would be adopted by a new profile of the same name.
                getattr(cls, "drop_live_session", lambda _name: None)(name)
                result.login_forgotten = True
            except Exception as exc:  # noqa: BLE001 - best effort, never fatal
                result.warnings.append(f"could not forget saved login: {exc}")
        # A backend that keeps its own copy of a sign-in in the profile's data folder (NIKKE)
        # drops it here, even when the rest of that folder is kept.
        forget_files = getattr(cls, "forget_session_files", None) if cls is not None else None
        if forget_files is not None:
            try:
                if forget_files(name):
                    result.login_forgotten = True
            except OSError as exc:
                result.warnings.append(f"could not forget saved login: {exc}")

        # 5b. Profile data folder, strictly inside profiles_dir().
        if delete_data:
            target = paths.profiles_dir() / name
            if target.is_symlink():
                result.warnings.append(f"refusing to delete symlink: {target}")
            elif target.exists():
                root = paths.profiles_dir().resolve()
                try:
                    resolved = target.resolve()
                    inside = resolved.is_relative_to(root)
                except OSError:
                    inside = False
                other = _shared_with(config.profiles, name, resolved) if inside else ""
                if not inside:
                    result.warnings.append(f"refusing to delete outside {root}: {target}")
                elif other:
                    result.warnings.append(f"not deleting data dir: shared with {other}")
                else:
                    # "Nadare's data" must not take the game with it: an install dir
                    # or prefix that lives inside the data dir stays unless the user
                    # ticked it (those are removed by step 4).
                    chosen = set(requested)
                    keep = [
                        t.path
                        for t in removable_trees(config, name)
                        if t.key not in chosen and t.path.resolve().is_relative_to(resolved)
                    ]
                    keep += [
                        b for b in unrecorded
                        if b.resolve().is_relative_to(resolved) and b not in keep
                    ]
                    try:
                        if keep:
                            outcome = _rmtree_safely(resolved, keep)
                            for kept in keep:
                                result.warnings.append(f"kept the game files in {kept}")
                        else:
                            outcome = _rmtree_safely(target)
                        for mount in outcome.mounts:
                            result.warnings.append(
                                f"{mount} is a separate mounted drive, left alone"
                            )
                        for path, exc in outcome.leftovers:
                            result.warnings.append(_leftover_warning(path, exc))
                            keep_profile = True
                        if not target.exists():
                            result.data_dir_removed = target
                    except OSError as exc:
                        result.warnings.append(f"could not remove data dir: {exc}")

    # The owned VKD3D cache is disposable even when the profile's data is kept.
    # Check every component below cache_dir: resolving a linked parent could delete
    # another game's cache or user files. Never follow profile environment overrides.
    if not keep_profile:
        try:
            target = shadercache.vkd3d_dir(name)
            relative = target.relative_to(paths.cache_dir())
        except ValueError:
            target = None
            result.warnings.append(
                f"not deleting shader cache: {name!r} is not a valid profile name"
            )
        if target is not None:
            try:
                current = paths.cache_dir()
                linked = current.is_symlink()
                for part in relative.parts:
                    current = current / part
                    linked = linked or current.is_symlink()
                if linked:
                    result.warnings.append(f"refusing to delete linked shader cache: {target}")
                elif target.is_dir():
                    outcome = _rmtree_safely(target)
                    for mount in outcome.mounts:
                        result.warnings.append(
                            f"shader cache {mount} is a mounted drive, left alone"
                        )
                    for path, exc in outcome.leftovers:
                        result.warnings.append(_leftover_warning(path, exc))
            except OSError as exc:
                result.warnings.append(f"could not remove shader cache: {exc}")

    # 5b2. The game's own log folder (``logs/<game-id>``) goes with its data; nothing else in
    # the logs folder does. A kept profile keeps its logs too: they are the evidence of
    # whatever was left behind (#972). Mounted drives inside are never entered.
    if delete_data and not keep_profile:
        log_folders = [paths.logs_dir() / runlog.game_id(name)]
        legacy = paths.logs_dir() / name
        if legacy != log_folders[0] and legacy.exists():
            # A profile that predates the hashed ids kept its logs in ``logs/<name>``.
            log_folders.append(legacy)
        for log_folder in log_folders:
            if log_folder.is_symlink():
                result.warnings.append(f"refusing to delete symlink: {log_folder}")
            elif log_folder.is_dir():
                try:
                    outcome = _rmtree_safely(log_folder)
                    for mount in outcome.mounts:
                        result.warnings.append(
                            f"{mount} is a separate mounted drive, left alone"
                        )
                    for path, exc in outcome.leftovers:
                        result.warnings.append(_leftover_warning(path, exc))
                    if not log_folder.exists():
                        result.logs_removed = log_folder
                except OSError as exc:
                    result.warnings.append(f"could not remove the logs: {exc}")

    # 5c. The per-game folder Nadare made around them goes too, but only when it is now empty
    # (``rmdir`` never touches contents), so a file the user put there survives. The empty
    # ``Nadare`` parent it was created in goes with it, unless it turns out to be home.
    if done and not keep_profile:
        home = Path.home().resolve()
        for entry in wrappers:
            folder = Path(str(entry))
            if not (folder.is_absolute() and not folder.is_symlink()):
                continue
            _drop_lone_manifest(folder)
            try:
                folder.rmdir()
            except FileNotFoundError:
                pass  # for some kinds ``folder`` is the tree step 4 just deleted
            except OSError:
                continue
            parent = folder.parent
            if parent.name != "Nadare" or not parent.is_absolute():
                continue
            try:
                if parent == Path("/") or parent.resolve() == home:
                    continue
            except OSError:
                continue
            try:
                parent.rmdir()
            except OSError:
                pass

    # 6. Drop the profile and persist.
    result.profile_kept = keep_profile
    if keep_profile and not touched and not (
        result.shortcuts_removed
        or result.login_forgotten
        or result.data_dir_removed
        or result.logs_removed
        or result.trees_removed
    ):
        # Every requested tree was refused (unsafe, missing): nothing was touched (round 6).
        result.refused = True
    if drop_profile and not keep_profile:
        config.remove_profile(name)
        if save:
            config.save()

    return result


__all__ = [
    "RemovableTree",
    "RemovalResult",
    "default_prefix",
    "remove_game",
    "removable_trees",
    "tree_safety",
]
