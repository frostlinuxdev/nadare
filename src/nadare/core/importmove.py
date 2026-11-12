# SPDX-License-Identifier: GPL-3.0-or-later
"""Move a game the user already had into a library (#1229).

An imported game lives where the user installed it: a game folder somewhere and a Wine prefix
somewhere else. Moving it into a library gives it the Steam-like layout
``<library>/Nadare/<Game>/{install,prefix}``; from then on it is a library game like any other.

:func:`plan` checks everything first (nothing is touched), :func:`run` moves the folders with
:func:`relocate.move_tree` (an instant rename on the same disk; on another disk a checked copy,
the original kept) and undoes the folders it already moved when a later one fails or is
cancelled, and :func:`commit` records the new places.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import libraries, relink, relocate, removal
from .events import Cancel


@dataclass(frozen=True)
class ImportMove:
    home: Path
    #: ``(profile key, source, destination)`` in the order they move.
    trees: tuple[tuple[str, Path, Path], ...]
    #: Where the game folder ends up when it sits inside the prefix (launcher games).
    install_inside: Path | None = None


def plan(config, name: str, library: str | Path) -> ImportMove:
    """What moving the import ``name`` into ``library`` does; raises :class:`relink.RelinkError`."""
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        raise relink.RelinkError("The game is not in your library.")
    if relink.managed_home(profile) is not None:
        raise relink.RelinkError(
            "This game is not an existing install you added; use Move to move it."
        )
    relink.check_place(library, library=True)
    root = relink._absolute(library)
    prefix_text = str(profile.get("prefix") or "")
    prefix = relink._absolute(prefix_text) if prefix_text else None
    if prefix is None or not prefix.is_dir() or not relink._has_drive_c(prefix):
        raise relink.RelinkError(
            "The game's Wine prefix was not found. If you moved it yourself, use "
            '"I moved it myself" first.'
        )
    install_text = str(profile.get("install_dir") or "")
    install = relink._absolute(install_text) if install_text else None
    inside = relink._under(install, prefix) if install is not None else None
    if install is not None and inside is None and not install.is_dir():
        raise relink.RelinkError(f"The game folder {install} was not found.")
    home = relink.free_home(
        root, libraries.game_folder_name(str(profile.get("title") or name)), config.profiles
    )
    trees: list[tuple[str, Path, Path]] = []
    if install is not None and inside is None:
        trees.append(("install_dir", install, home / "install"))
    trees.append(("prefix", prefix, home / "prefix"))
    for _, source, _ in trees:
        if relink._under(root, source) is not None:
            raise relink.RelinkError(
                "That library is inside the game's own folder. Pick another one."
            )
        who = relink._other_inside(config.profiles, name, source)
        if who:
            raise relink.RelinkError(
                f"{who} keeps its files inside this game's folder, so it would be moved "
                f"along. Move or remove {who} first."
            )
    who = relink._inside_other(config.profiles, name, root)
    if who:
        raise relink.RelinkError(f"That folder is inside {who}'s files. Pick another one.")
    return ImportMove(home, tuple(trees), home / "prefix" / inside if inside is not None else None)


def run(
    move: ImportMove,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[relocate.Moved]:
    """Move every folder; on any failure put the ones already moved back, then re-raise."""
    done: list[tuple[Path, Path, relocate.Moved]] = []
    try:
        for _, source, destination in move.trees:
            result = relocate.move_tree(source, destination, cancel=cancel, progress=progress)
            done.append((source, destination, result))
    except BaseException:
        for source, destination, result in reversed(done):
            _undo(source, destination, result)
        _remove_if_empty(move.home)
        raise
    return [result for _, _, result in done]


def _undo(source: Path, destination: Path, result: relocate.Moved) -> None:
    if result.renamed:
        # Same disk: the user's only copy is at ``destination`` now; rename it back.
        os.rename(destination, source)
    elif result.kept_old is not None and Path(result.kept_old).exists():
        # A copy, with the original still in place: only Nadare's copy goes.
        removal._rmtree_safely(destination)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def relink_plan(profile: dict, move: ImportMove) -> relink.Relink:
    """The profile changes after :func:`run`: the new places, owned by Nadare from now on."""
    changes: dict = {}
    moved = [key for key, _, _ in move.trees]
    for key, _, destination in move.trees:
        if key == "prefix" and str(profile.get("installed_prefix") or "") == str(
            profile.get("prefix") or ""
        ):
            changes["installed_prefix"] = str(destination)
        changes[key] = str(destination)
    if move.install_inside is not None:
        changes["install_dir"] = str(move.install_inside)
    changes["owned_dirs"] = [str(move.home)]
    changes["owned_trees"] = moved
    changes["owned_paths"] = {key: str(Path(changes[key]).resolve()) for key in moved}
    notes = [f"Game folder: {move.home}"]
    return relink.Relink(changes=changes, notes=notes)


def commit(config, name: str, move: ImportMove) -> None:
    """Record the new places, write the manifest and list the library. The caller saves."""
    relink.apply(config.profiles[name], relink_plan(config.profiles[name], move))
    libraries.sync_game(config, name, remember_library=True)


__all__ = ["ImportMove", "commit", "plan", "relink_plan", "run"]
