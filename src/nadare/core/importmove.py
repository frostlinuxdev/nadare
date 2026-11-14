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

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import libraries, relink, relocate, removal
from .events import Cancel

log = logging.getLogger(__name__)


def _journal_path(config) -> Path:
    path = Path(os.path.realpath(getattr(config, "path", "config.json")))
    return path.with_name(f".{path.name}.importmove.json")


def _write_journal(move: ImportMove) -> None:
    if move.journal is None:
        return
    payload = {
        "profile": move.name,
        "library": str(move.library) if move.library is not None else "",
        "home": str(move.home),
        "install_inside": str(move.install_inside) if move.install_inside is not None else None,
        "trees": [
            {"key": key, "source": str(source), "destination": str(destination)}
            for key, source, destination in move.trees
        ],
    }
    move.journal.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(move.journal.parent), prefix=".importmove-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, move.journal)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _clear_journal(move: ImportMove) -> None:
    if move.journal is None:
        return
    try:
        move.journal.unlink(missing_ok=True)
    except OSError:
        log.debug("could not clear import-move journal %s", move.journal)


def _read_journal(config) -> dict | None:
    path = _journal_path(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return Path(os.path.abspath(a)) == Path(os.path.abspath(b))


def _recover_move(config, name: str, library: str | Path) -> ImportMove | None:
    """The half-done move recorded before a crash, if it belongs to this profile/library."""
    journal = _read_journal(config)
    if journal is None or journal.get("profile") != name:
        return None
    root = relink._absolute(library)  # noqa: SLF001 - same package's path rule
    stored_library = journal.get("library")
    home_text = journal.get("home")
    if not isinstance(stored_library, str) or not stored_library:
        return None
    if not _same_path(relink._absolute(stored_library), root):
        return None  # the interrupted move went to another library
    if not isinstance(home_text, str) or not home_text:
        return None
    home = Path(home_text)
    if not home.is_absolute():
        return None
    raw_trees = journal.get("trees")
    if not isinstance(raw_trees, list) or not raw_trees:
        return None
    profile = config.profiles.get(name, {})
    trees: list[tuple[str, Path, Path]] = []
    done: list[str] = []
    for item in raw_trees:
        if not isinstance(item, dict):
            return None
        key, source_text, destination_text = (
            item.get("key"),
            item.get("source"),
            item.get("destination"),
        )
        if key not in ("install_dir", "prefix", "wineprefix"):
            return None
        if not isinstance(source_text, str) or not isinstance(destination_text, str):
            return None
        source, destination = Path(source_text), Path(destination_text)
        if not source.is_absolute() or not destination.is_absolute():
            return None
        if relink._under(destination, home) is None:  # noqa: SLF001 - journal from this move
            return None
        current = str(profile.get(key) or "")
        if current and not _same_path(Path(current).expanduser(), source):
            return None  # the profile changed since the journal was written
        if destination.exists() or destination.is_symlink():
            done.append(str(key))
        elif not source.is_dir():
            return None  # neither half is there: not a move this journal can finish
        trees.append((str(key), source, destination))
    inside_text = journal.get("install_inside")
    inside = Path(inside_text) if isinstance(inside_text, str) and inside_text else None
    return ImportMove(
        home,
        tuple(trees),
        inside,
        name=name,
        library=root,
        journal=_journal_path(config),
        done=tuple(done),
    )


@dataclass(frozen=True)
class ImportMove:
    home: Path
    #: ``(profile key, source, destination)`` in the order they move.
    trees: tuple[tuple[str, Path, Path], ...]
    #: Where the game folder ends up when it sits inside the prefix (launcher games).
    install_inside: Path | None = None
    #: Profile and library this move belongs to, plus its crash journal.
    name: str = ""
    library: Path | None = None
    journal: Path | None = None
    #: Trees an earlier, killed run already put in place.
    done: tuple[str, ...] = ()


def plan(config, name: str, library: str | Path) -> ImportMove:
    """What moving the import ``name`` into ``library`` does; raises :class:`relink.RelinkError`."""
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        raise relink.RelinkError("The game is not in your library.")
    if relink.managed_home(profile) is not None:
        raise relink.RelinkError(
            "This game is not an existing install you added; use Move to move it."
        )
    recovered = _recover_move(config, name, library)
    if recovered is not None:
        return recovered
    relink.check_library_target(library)
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
    return ImportMove(
        home,
        tuple(trees),
        home / "prefix" / inside if inside is not None else None,
        name=name,
        library=root,
        journal=_journal_path(config),
    )


def run(
    move: ImportMove,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[relocate.Moved]:
    """Move every folder; on any failure put the ones already moved back, then re-raise."""
    try:
        _write_journal(move)
    except OSError as exc:
        raise relocate.RelocateError(
            f"Could not record the move before starting it: {exc}"
        ) from exc
    done: list[tuple[Path, Path, relocate.Moved]] = []
    results: list[relocate.Moved] = []
    try:
        for key, source, destination in move.trees:
            if key in move.done:
                # A killed earlier run already finished this tree; finish the rest.
                results.append(relocate.Moved(renamed=True, kept_old=None))
                continue
            result = relocate.move_tree(source, destination, cancel=cancel, progress=progress)
            done.append((source, destination, result))
            results.append(result)
    except BaseException:
        for source, destination, result in reversed(done):
            _undo(source, destination, result)
        _remove_if_empty(move.home)
        if not move.done:
            _clear_journal(move)  # everything was rolled back
        raise
    # The journal stays until commit() has recorded the new places: a kill between the
    # last move and commit must still be finishable.
    return results


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
    _clear_journal(move)


__all__ = ["ImportMove", "commit", "plan", "relink_plan", "run"]
