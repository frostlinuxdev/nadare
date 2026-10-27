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
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import backends, paths, shortcuts

_TREE_KEYS = ("install_dir", "prefix", "wineprefix")


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


def _rmtree_except(root: Path, keep: list[Path]) -> None:
    """Delete all of ``root`` except the ``keep`` trees and the folders leading to them."""
    for entry in root.iterdir():
        resolved = entry.resolve() if not entry.is_symlink() else entry
        if any(resolved == k or k in resolved.parents for k in keep):
            continue  # inside or equal to a kept tree
        if any(resolved in k.parents for k in keep):
            _rmtree_except(resolved, keep)  # on the way to a kept tree
        elif entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def _created_by_nadare(profile, key: str, path: Path, name: str) -> bool:
    """Whether Nadare made ``path``, so deleting it cannot take anything of the user's.

    True for the profile's own folder under Nadare's data dir, for a key the Add game dialog
    recorded in ``owned_trees`` (the folder was new or empty when the game was added), and for
    a Wine prefix (a ``drive_c`` folder, dedicated to one game by nature). A folder the user
    picked, such as a games library holding one game, is never offered for deletion.
    """
    owned = profile.get("owned_trees")
    if isinstance(owned, list) and key in owned:
        return True
    if key in ("prefix", "wineprefix") and (path / "drive_c").is_dir():
        return True
    try:
        own = (paths.profiles_dir() / name).resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == own or own in resolved.parents


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
        if safe and not _created_by_nadare(profile, key, expanded, name):
            safe = False
            reason = (
                "Nadare did not create this folder, so it will not delete it "
                "(remove it yourself if you want it gone)"
            )
        if expanded.is_symlink():
            shown = expanded
        else:
            try:
                shown = expanded.resolve()
            except OSError:
                shown = expanded
        found.append(RemovableTree(key=key, path=shown, safe=safe, reason=reason))
    return found


def remove_game(
    config,
    name: str,
    *,
    delete_data: bool = False,
    delete_trees: Iterable[str] = (),
    applications_dir=None,
    desktop_dir=None,
    save: bool = True,
    drop_profile: bool = True,
) -> RemovalResult:
    """Remove ``name`` from ``config`` and clean up its shortcuts/login/data.

    ``drop_profile=False`` skips the config mutation so the GUI can run the slow
    parts (keyring, deleting a prefix) on a worker thread and drop the profile
    itself on the GUI thread.
    """
    if name not in config.profiles:
        raise backends.BackendError(f"no such profile: {name!r}")

    result = RemovalResult(profile=name)

    # 1. Refuse everything when any requested tree deletion is unsafe: the
    # later steps are irreversible, so nothing is touched.
    requested = list(dict.fromkeys(delete_trees))
    trees: list[RemovableTree] = []
    if requested:
        by_key = {t.key: t for t in removable_trees(config, name)}
        trees = [by_key[k] for k in requested if k in by_key]
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
        if missing:
            labels = {
                "install_dir": "install folder",
                "prefix": "Wine prefix",
                "wineprefix": "Wine prefix",
            }
            for key in missing:
                result.warnings.append(
                    f"no configured {labels.get(key, key)} was found for {name}; "
                    "nothing was deleted"
                )
            result.profile_kept = True
            return result
        refused = [t for t in trees if not t.safe]
        if refused:
            for t in refused:
                result.warnings.append(f"not deleting {t.key}: {t.reason}")
            result.profile_kept = True
            return result

    # 2. Before deleting anything, prove the config can be persisted. Otherwise a
    # read-only config directory would leave the files gone but the profile on
    # disk. A no-op save is the cheapest such preflight.
    if drop_profile and save:
        try:
            config.save()
        except OSError as exc:
            result.warnings.append(f"could not save config: {exc}")
            result.profile_kept = True
            return result

    # 3. Desktop/menu shortcuts owned by this profile.
    result.shortcuts_removed = shortcuts.remove_desktop_entries(
        name, applications_dir=applications_dir, desktop_dir=desktop_dir
    )

    steam_files, steam_warnings = shortcuts.remove_steam_shortcuts(name)
    result.shortcuts_removed += steam_files
    result.warnings += steam_warnings

    # 4. Saved login, if the backend supports remembering one.
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

    # 5. Profile data folder, strictly inside profiles_dir().
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
                # ticked it (those are removed by step 4b).
                chosen = set(requested)
                keep = [
                    t.path
                    for t in removable_trees(config, name)
                    if t.key not in chosen and t.path.resolve().is_relative_to(resolved)
                ]
                try:
                    if keep:
                        _rmtree_except(resolved, [k.resolve() for k in keep])
                        for kept in keep:
                            result.warnings.append(f"kept the game files in {kept}")
                    else:
                        shutil.rmtree(target)
                    result.data_dir_removed = target
                except OSError as exc:
                    result.warnings.append(f"could not remove data dir: {exc}")

    # 5b. Opt-in deletion of the configured install dir / prefix trees.
    keep_profile = False
    done: list[Path] = []
    # Outermost first; a tree already gone with its parent is not an error.
    for tree in sorted(trees, key=lambda t: len(t.path.parts)):
        if any(d == tree.path or d in tree.path.parents for d in done):
            continue
        try:
            shutil.rmtree(tree.path)
            result.trees_removed.append(tree.path)
            done.append(tree.path)
        except FileNotFoundError:
            done.append(tree.path)
        except OSError as exc:
            result.warnings.append(f"could not remove {tree.key}: {exc}")
            keep_profile = True

    # 5c. The per-game folder Nadare made around them goes too, but only when it is now empty
    # (``rmdir`` never touches contents), so a file the user put there survives.
    if done and not keep_profile:
        owned_dirs = config.profiles.get(name, {}).get("owned_dirs")
        for entry in owned_dirs if isinstance(owned_dirs, list) else ():
            folder = Path(str(entry))
            if folder.is_absolute() and not folder.is_symlink():
                try:
                    folder.rmdir()
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
