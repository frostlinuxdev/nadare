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

import os
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import backends, paths, process, shortcuts

_TREE_KEYS = ("install_dir", "prefix", "wineprefix")
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
    warnings: list[str] = field(default_factory=list)
    trees_removed: list[Path] = field(default_factory=list)
    #: True when a requested deletion was refused/failed and the profile was kept.
    profile_kept: bool = False


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
    """Name of another profile whose configured tree overlaps ``resolved`` ("" if none)."""
    for other, other_profile in config_profiles.items():
        if other == name:
            continue
        for key in _TREE_KEYS:
            value = _setting(other_profile, key)
            if not value or not isinstance(value, str):
                continue
            try:
                other_resolved = Path(value).expanduser().resolve()
            except OSError:
                continue
            if (
                other_resolved == resolved
                or resolved in other_resolved.parents
                or other_resolved in resolved.parents
            ):
                return other
    return ""


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
        own = (paths.profiles_dir() / name).resolve()
    except OSError:
        own = None
    for nd in _nadare_dirs():
        if resolved == nd or resolved in nd.parents:
            return False, "is inside nadare's own directories"
        if nd in resolved.parents:
            # Only this profile's own folder (and below) is deletable.
            if own is not None and (resolved == own or own in resolved.parents):
                continue
            return False, "is inside nadare's own directories"
    other = _shared_with(config_profiles, name, resolved)
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


def _mountinfo_points() -> frozenset[str]:
    """Every mount point the kernel lists, including same-filesystem bind mounts."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return _parse_mountinfo(handle.read())
    except OSError:
        return frozenset()


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
        return False


def _has_mount_below(root: Path, mounts: _Mounts) -> bool:
    """Whether any folder below ``root`` is a separate mounted drive (symlinks not followed)."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return False
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        if _is_mount(path, mounts) or _has_mount_below(path, mounts):
            return True
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
    """Delete ``root``'s contents, leaving mounts and the ``keep`` trees in place."""
    for entry in root.iterdir():
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
            _rmtree_contents(entry, mounts, keep, kept, failed)
            try:
                entry.rmdir()
            except OSError as exc:
                # A mount or a kept tree inside it explains the failure; anything else is
                # a folder the user has to remove by hand.
                if not _explained_rmdir_failure(entry, keep, kept, failed):
                    failed.append((entry, exc))
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
    keep = [k.resolve() for k in keep]
    try:
        root_dev = root.stat().st_dev
    except OSError:
        root_dev = -1
    mounts = _Mounts(root_dev, _mountinfo_points())
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
        own = (paths.profiles_dir() / name).resolve()
        resolved = path.resolve()
    except OSError:
        return _NOT_CREATED
    owned = profile.get("owned_trees")
    if isinstance(owned, list) and key in owned:
        recorded = profile.get("owned_paths")
        if not isinstance(recorded, dict) or key not in recorded:
            return ""  # profile made before paths were recorded (0.1.0b13)
        try:
            same = Path(str(recorded[key])).resolve() == resolved
        except (OSError, RuntimeError):
            same = False
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


def _created_by_nadare(profile, key: str, path: Path, name: str) -> bool:
    """Whether Nadare made ``path``, so deleting it cannot take anything of the user's."""
    return not _not_owned_reason(profile, key, path, name)


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


def removable_trees(config, name: str) -> list[RemovableTree]:
    """List the install/prefix trees configured for ``name`` and whether each is safe to delete."""
    profile = config.profiles.get(name, {})
    found: list[RemovableTree] = []
    for key in _TREE_KEYS:
        value = _setting(profile, key)
        if not value or not isinstance(value, str):
            continue
        expanded = Path(value).expanduser()
        if not expanded.is_dir():
            continue
        safe, reason = tree_safety(config.profiles, name, expanded)
        if safe:
            reason = _not_owned_reason(profile, key, expanded, name)
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


def _wrapper_folders(profile: dict) -> list[str]:
    """The per-game folders Nadare made around the game: ``owned_dirs``, else the Add Game shape.

    A profile older Nadare made recorded no ``owned_dirs``; its owned ``prefix`` and ``install``
    folders still sit in ``<library>/Nadare/<Game>``, which is tidied the same way once empty.
    """
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list) and dirs:
        return [str(d) for d in dirs]
    owned = profile.get("owned_trees")
    prefix, install = (
        Path(str(profile.get(k) or "")).expanduser() for k in ("prefix", "install_dir")
    )
    if not isinstance(owned, list) or "prefix" not in owned or prefix.name != "prefix":
        return []
    home = prefix.parent
    if home.parent.name != "Nadare":
        return []
    if profile.get("install_dir") and (install != home / "install" or "install_dir" not in owned):
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
    except process.ProfileLockError:
        raise backends.BackendError(process.OPERATION_BUSY.format(name=name)) from None
    # This separate profile lock stops another Nadare command (such as Move)
    # from overlapping the deletion; it does not stop a game from launching, so
    # ``_running_problem`` is checked again right before the first deletion.
    game_lock = process.ProfileLock(name)
    try:
        try:
            game_lock.acquire()
        except process.ProfileLockError:
            result = RemovalResult(profile=name, profile_kept=True)
            result.warnings.append(
                "this game's profile is in use by another Nadare command; nothing was deleted"
            )
            return result
        problem = _running_problem(config, name)
        if problem is not None:
            result = RemovalResult(profile=name, profile_kept=True)
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
    try:
        backend = backends.create(name, config)
    except Exception:  # noqa: BLE001 - no loadable backend: nothing of Nadare's can be running
        return None
    try:
        state = getattr(backend.status(), "state", "")
    except Exception as exc:  # noqa: BLE001 - unknown means do not delete
        return f"could not check whether the game is running ({exc}); nothing was deleted"
    if state == "running":
        return "the game is running (stop it first); nothing was deleted"
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

    # 1. Check every requested tree first. A missing or unsafe one is skipped
    # with a warning (and the profile is kept), but it must not cancel the safe
    # trees the user also ticked.
    requested = list(dict.fromkeys(delete_trees))
    trees: list[RemovableTree] = []
    keep_profile = False
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
        try:
            config.save()
        except OSError as exc:
            result.warnings.append(f"could not save config: {exc}")
            result.profile_kept = True
            return result

    # 3. The game could have started since the probe in ``remove_game``; a launch
    # takes no lock of ours. Check again right before the first irreversible
    # step, and on any doubt keep the profile and change nothing.
    problem = _running_problem(config, name)
    if problem is not None:
        result.warnings.append(problem)
        result.profile_kept = True
        return result

    # 4. Opt-in deletion of the configured install dir / prefix trees. These go
    # first: only when every requested tree was handled without a skip or a
    # failure may the shortcuts, login and data folder follow.
    done: list[Path] = []
    # Outermost first; a tree already gone with its parent is not an error.
    for tree in sorted(trees, key=lambda t: len(t.path.parts)):
        if any(d == tree.path or d in tree.path.parents for d in done):
            continue
        try:
            outcome = _rmtree_safely(tree.path)
            for mount in outcome.mounts:
                result.warnings.append(f"{mount} is a separate mounted drive, left alone")
            if outcome.mounts:
                keep_profile = True  # the tree was only partly removed
            for path, exc in outcome.leftovers:
                result.warnings.append(_leftover_warning(path, exc))
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
                result.login_forgotten = True
            except Exception as exc:  # noqa: BLE001 - best effort, never fatal
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

    # 5c. The per-game folder Nadare made around them goes too, but only when it is now empty
    # (``rmdir`` never touches contents), so a file the user put there survives. The empty
    # ``Nadare`` parent it was created in goes with it, unless it turns out to be home.
    if done and not keep_profile:
        home = Path.home().resolve()
        for entry in _wrapper_folders(config.profiles.get(name, {})):
            folder = Path(str(entry))
            if not (folder.is_absolute() and not folder.is_symlink()):
                continue
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
    if drop_profile and not keep_profile:
        config.remove_profile(name)
        if save:
            config.save()

    return result


__all__ = ["RemovableTree", "RemovalResult", "remove_game", "removable_trees", "tree_safety"]
