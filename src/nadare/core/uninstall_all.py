# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Uninstall everything Nadare put on this computer, group by group.

Safety rules, in the order they are applied:

* Nothing is deleted while a game is running. Every profile is checked first, and one
  problem refuses the whole run before the config file or any folder is touched.
* Games go through :func:`nadare.core.removal.remove_game`, so shortcuts, saved logins,
  ownership checks, mounted drives and symlinks behave exactly as in Remove.
* Nadare's own folders are deleted with :func:`nadare.core.removal._rmtree_safely`.
  Game folders, prefixes and backups the user asked to keep are passed as ``keep`` trees.
* A profile that Remove keeps (because a requested deletion was refused or failed) stops
  the run before Nadare's folders are deleted: they may still hold that game's data.
* ``run()`` never deletes anything unless the caller opts in; ``plan()`` only looks.

The command without ``--yes`` prints the plan and deletes nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths, process, removal

#: Tree keys ``remove_game`` understands for the game's own files.
_GAME_TREE_KEYS = ("install_dir", "prefix", "wineprefix")


@dataclass(frozen=True)
class Group:
    """One part of an uninstall, with the folders it would delete."""

    key: str
    label: str
    default: bool
    paths: tuple[Path, ...]
    note: str = ""


@dataclass
class Report:
    """What an uninstall did, or why it stopped."""

    profiles_removed: list[str]
    removed: list[Path]
    warnings: list[str]
    refused: bool = False


def _existing(values: list[Path]) -> tuple[Path, ...]:
    """The paths that exist, without duplicates, in the order given."""
    out: list[Path] = []
    for path in values:
        if path.exists() and path not in out:
            out.append(path)
    return tuple(out)


def _is_backup_tree(tree: removal.RemovableTree) -> bool:
    return tree.key == removal._BACKUP_KEY or tree.key.startswith(f"{removal._BACKUP_KEY}:")


def plan(config) -> list[Group]:
    """Describe the four uninstall groups without deleting anything."""
    nadare = _existing(
        [paths.config_dir(), paths.state_dir(), paths.cache_dir(), paths.data_dir()]
    )
    runtimes = _existing([paths.runners_dir(), paths.data_dir() / "umu"])

    games: list[Path] = []
    backups: list[Path] = []
    for name in config.profiles:
        for tree in removal.removable_trees(config, name):
            if not tree.safe:
                continue
            if _is_backup_tree(tree):
                if tree.path not in backups:
                    backups.append(tree.path)
            elif tree.path not in games:
                games.append(tree.path)

    return [
        Group(
            key="nadare",
            label="Nadare's settings, logs, cache and data",
            default=True,
            paths=nadare,
        ),
        Group(
            key="runtimes",
            label="Downloaded Proton/Wine runners and the managed umu folder",
            default=True,
            paths=runtimes,
        ),
        Group(
            key="games",
            label="Game folders and prefixes Nadare made",
            default=False,
            paths=tuple(games),
            note="folders Nadare did not create are never deleted",
        ),
        Group(
            key="backups",
            label="Old prefix backups recorded after rebuilding a prefix",
            default=False,
            paths=tuple(backups),
            note="only backups Remove would delete are listed",
        ),
    ]


def _configured_game_paths(config) -> list[Path]:
    """Every existing install dir / prefix the profiles point at, safe or not."""
    out: list[Path] = []
    for name, profile in config.profiles.items():
        for key in _GAME_TREE_KEYS:
            value = removal._setting(profile, key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value).expanduser()
            if path.exists() and path not in out:
                out.append(path)
        default = removal.default_prefix(name)
        if default is not None and default not in out:
            out.append(default)
    return out


def _recorded_backup_paths(config) -> list[Path]:
    """Every existing recorded backup path, before the profiles are dropped."""
    out: list[Path] = []
    for profile in config.profiles.values():
        for value in removal._recorded_backups(profile):
            path = Path(value).expanduser()
            if path.exists() and path not in out:
                out.append(path)
    return out


def _applications_dir() -> Path:
    """The menu folder Remove uses when no override is passed."""
    from . import shortcuts

    return shortcuts._applications_dir(None)


def _desktop_dir() -> Path:
    """The Desktop folder Remove uses when no override is passed."""
    from . import shortcuts

    return shortcuts.desktop_dir()


def _delete_root(root: Path, keep: list[Path], removed: list[Path], warnings: list[str]) -> None:
    """Delete one of Nadare's own folders with the mount-aware helper."""
    if root.is_symlink():
        # A folder moved to another disk and linked back: walking it would delete whatever
        # else lives at the link's target. Leave it, and say so.
        warnings.append(
            f"{root} is a link to {os.path.realpath(root)}; it was left alone. Remove "
            "Nadare's files there yourself if you want them gone."
        )
        return
    if not root.exists():
        return
    # ``_rmtree_safely`` leaves the root itself when it is given any keep tree, even one
    # outside this root. Only trees inside this root can matter here, so pass just those;
    # without any, an emptied root is removed too.
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    root_keep: list[Path] = []
    for path in keep:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            root_keep.append(path)
    try:
        outcome = removal._rmtree_safely(root, keep=root_keep)
    except OSError as exc:
        warnings.append(f"could not remove {root}: {exc}")
        return
    for mount in outcome.mounts:
        warnings.append(f"{mount} is a separate mounted drive and was left alone")
    for path, exc in outcome.leftovers:
        warnings.append(removal._leftover_warning(path, exc))
    if not root.exists():
        removed.append(root)


def run(
    config,
    *,
    games: bool = False,
    backups: bool = False,
    runtimes: bool = True,
    applications_dir: Any = None,
    desktop_dir: Any = None,
) -> Report:
    """Remove every profile, then Nadare's own folders, as requested.

    A running game or a profile Remove keeps stops the run before Nadare's folders are
    touched. The caller is responsible for asking the user first.
    """
    if applications_dir is None:
        applications_dir = _applications_dir()
    if desktop_dir is None:
        desktop_dir = _desktop_dir()

    warnings: list[str] = []

    # 1. Before anything is touched, prove no game is running. One problem refuses the
    # whole run, so a running game never loses its profile or another game's data.
    problems: list[str] = []
    for name in list(config.profiles):
        problem = removal._running_problem(config, name)
        if problem is None and process.operation_in_progress(name):
            # An install or update is writing: Remove would refuse this game later, after
            # earlier games were already gone.
            problem = process.OPERATION_BUSY.format(name=name)
        if problem is not None:
            problems.append(problem)
    if problems:
        return Report(profiles_removed=[], removed=[], warnings=problems, refused=True)

    # Capture what has to survive before the profiles are dropped: which safe trees are
    # game folders (and so deletable only with ``games``), and which folders/backups to
    # keep while Nadare's own folders are removed.
    groups = {group.key: group for group in plan(config)}
    game_paths = list(groups["games"].paths)
    configured_paths = _configured_game_paths(config)
    backup_paths = _recorded_backup_paths(config)
    names = list(config.profiles)
    # Which trees each game may lose, decided now: once a profile is dropped, a folder it shared
    # with the next one no longer looks shared, and it was never in the plan (#1058 review).
    deletable = {
        name: tuple(
            tree.key
            for tree in removal.removable_trees(config, name)
            if tree.safe and not _is_backup_tree(tree)
        )
        for name in names
    }

    # 2. Every profile goes through the normal per-game removal.
    removed_profiles: list[str] = []
    kept_profile = False
    for name in names:
        delete_trees: tuple[str, ...] = ()
        if games:
            delete_trees = deletable[name]
        result = removal.remove_game(
            config,
            name,
            delete_data=True,
            delete_trees=delete_trees,
            delete_backups=backups,
            applications_dir=applications_dir,
            desktop_dir=desktop_dir,
            save=True,
        )
        warnings.extend(result.warnings)
        if result.profile_kept:
            kept_profile = True
            warnings.append(
                f"{name} was kept; Nadare's own folders were left alone because they "
                "may still hold this game's data"
            )
        else:
            removed_profiles.append(name)

    if kept_profile:
        return Report(
            profiles_removed=removed_profiles,
            removed=[],
            warnings=warnings,
            refused=False,
        )

    # 3. What must survive the deletion of Nadare's own folders.
    keep: list[Path] = []
    if not games:
        keep.extend(game_paths)
        keep.extend(configured_paths)
    else:
        # Even with games chosen, a folder Nadare did not make is never deleted, wherever it is.
        keep.extend(path for path in configured_paths if path not in game_paths)
    if not backups:
        keep.extend(backup_paths)
    if not runtimes:
        keep.extend(groups["runtimes"].paths)

    # 4. Downloaded runners (and the managed umu folder) unless the user kept them.
    removed: list[Path] = []
    if runtimes:
        for root in groups["runtimes"].paths:
            _delete_root(root, keep, removed, warnings)

    # 5. Nadare's own settings, logs, cache and data.
    for root in groups["nadare"].paths:
        _delete_root(root, keep, removed, warnings)

    return Report(profiles_removed=removed_profiles, removed=removed, warnings=warnings)


__all__ = ["Group", "Report", "plan", "run"]
