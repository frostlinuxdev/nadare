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

import dataclasses
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import folderproof, libraries, pending, relink, relocate, removal
from .events import Cancel

log = logging.getLogger(__name__)


def _journal_path(config) -> Path:
    return pending.import_journal_path(getattr(config, "path", "config.json"))


_MISSING = object()


def _owner(path: Path) -> object:
    """The profile a journal belongs to; ``_MISSING`` when there is none, ``None`` if unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    owner = raw.get("profile") if isinstance(raw, dict) else None
    return owner if isinstance(owner, str) and owner else None


def _refuse_foreign(move: ImportMove, owner: object) -> None:
    """One unresolved import move per config: never overwrite another one's record (#1488)."""
    if owner is _MISSING or owner == move.name:
        return
    if owner is None:
        raise relocate.RelocateError(
            f"The record of an earlier move into a library ({move.journal}) could not be read; "
            "it was kept. Restart Nadare, then try again. Nothing was moved."
        )
    raise relocate.RelocateError(
        f"Moving {owner} into a library is unfinished; restart Nadare to finish it, then try "
        "again. Nothing was moved."
    )


def _write_journal(
    move: ImportMove, copies: dict[str, list[int]] | None = None, *, claim: bool = False
) -> None:
    """Write the journal; ``copies`` maps a copied tree's key to its copy's ``[dev, ino]``.

    Each tree also records its source's ``[dev, ino]`` and the token of the marker written
    into it before anything moves: a rename keeps both and a copy carries the marker, so
    recovery adopts or removes a destination only when it is still that folder (#1487, and
    folderproof: ext4 gives the inode number to a later folder).
    """
    if move.journal is None:
        return
    copies = copies or {}
    sources = move.sources
    payload = {
        "profile": move.name,
        "library": str(move.library) if move.library is not None else "",
        "home": str(move.home),
        "install_inside": str(move.install_inside) if move.install_inside is not None else None,
        "trees": [
            {
                "key": key, "source": str(source), "destination": str(destination),
                # A copy to another disk leaves the original: the next start removes the copy
                # only when it is still this very folder (#1400).
                **({"copy_id": copies[key]} if key in copies else {}),
                **({"source_id": sources[key]} if key in sources else {}),
                **({"token": move.tokens[key]} if key in move.tokens else {}),
            }
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
        if claim:
            # The first write claims the shared path atomically: a link fails when another
            # move (another window or process) has written its journal meanwhile.
            try:
                os.link(tmp, move.journal)
            except FileExistsError:
                _refuse_foreign(move, _owner(move.journal))
                os.replace(tmp, move.journal)  # our own journal: a resumed move
        else:
            _refuse_foreign(move, _owner(move.journal))
            os.replace(tmp, move.journal)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _clear_journal(move: ImportMove) -> None:
    if move.journal is None:
        return
    if _owner(move.journal) != move.name:
        return  # not this move's record (#1488): another profile's, unreadable or gone
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


def _id_pair(value) -> list[int] | None:
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        return list(value)
    return None


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
    copies: dict[str, list[int]] = {}
    sources: dict[str, list[int]] = {}
    tokens: dict[str, str] = {}
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
        copy_id, source_id = _id_pair(item.get("copy_id")), _id_pair(item.get("source_id"))
        token = item.get("token")
        if source_id is not None:
            sources[str(key)] = source_id
        if folderproof.valid_token(token):
            tokens[str(key)] = token
        if os.path.lexists(destination):
            # Only the very folder this move made counts as moved (#1487): the copy it
            # recorded, or the renamed source, each carrying the move's marker. A link, a
            # replacement, a folder that got the same inode number, or a journal without a
            # token (b25 and older) is not adopted, and nothing is touched.
            expected = copy_id if copy_id is not None else source_id
            if not folderproof.matches(
                destination, "import", folderproof.record_of(expected, token)
            ):
                raise Unverified(
                    f"{destination} is not the folder the move put there (it may have been "
                    "replaced or is a link)"
                )
            done.append(str(key))
            if copy_id is not None:
                copies[str(key)] = copy_id
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
        copies=copies,
        sources=sources,
        tokens=tokens,
    )


class Unverified(relink.RelinkError):
    """A journal names a destination that is not provably the folder the move made (#1487)."""


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
    #: Of those, the ones copied to another disk: ``{key: [dev, ino]}`` of the copy (#1400).
    copies: dict[str, list[int]] = field(default_factory=dict)
    #: Each tree's source ``{key: [dev, ino]}`` before it moved (#1487).
    sources: dict[str, list[int]] = field(default_factory=dict)
    #: Each tree's marker token (folderproof), written into the source before it moved.
    tokens: dict[str, str] = field(default_factory=dict)


def movable(config, name: str) -> bool:
    """Whether ``name`` is an import :func:`plan` could move (the library is checked later).

    Add Game offers the move only then (#1372 phase 2c): a game Nadare recorded a folder for,
    or an import whose prefix or game folder is missing, is never offered one.
    """
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        return False
    if relink.managed_home(profile) is not None or profile.get("owned_dirs"):
        return False
    prefix_text = str(profile.get("prefix") or "")
    if not prefix_text:
        return False
    prefix = relink._absolute(prefix_text)
    if not prefix.is_dir() or not relink._has_drive_c(prefix):
        return False
    install_text = str(profile.get("install_dir") or "")
    if install_text:
        install = relink._absolute(install_text)
        if relink._under(install, prefix) is None and not install.is_dir():
            return False
    return True


def plan(config, name: str, library: str | Path) -> ImportMove:
    """What moving the import ``name`` into ``library`` does; raises :class:`relink.RelinkError`."""
    profile = config.profiles.get(name)
    if not isinstance(profile, dict):
        raise relink.RelinkError("The game is not in your library.")
    if relink.managed_home(profile) is not None or profile.get("owned_dirs"):
        # A game Nadare recorded a folder for is not an import, even when that folder cannot
        # move as a whole (phase 1 step 4 narrowed managed_home to location's answer).
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
        sources=_source_ids(trees),
    )


def _source_ids(trees, done=()) -> dict[str, list[int]]:
    """``{key: [dev, ino]}`` of each tree's source that has not moved yet (#1487)."""
    found: dict[str, list[int]] = {}
    for key, source, _ in trees:
        if key not in done:
            identity = relocate._identity(source)  # noqa: SLF001 - same package
            if identity is not None:
                found[key] = list(identity)
    return found


def _stamp_sources(move: ImportMove) -> ImportMove:
    """Mark every tree that has not moved yet; the journal written next records the marks.

    The marker goes with the folder (rename) or is copied with it, so the next start can prove
    the destination is this folder (folderproof). One that cannot take it is never adopted or
    removed by a recovery (it is kept, and the player settles it).
    """
    sources, tokens = dict(move.sources), dict(move.tokens)
    for key, source, _ in move.trees:
        if key in move.done:
            continue
        proof = folderproof.try_stamp(source, "import")
        tokens.pop(key, None)
        if proof is not None:
            sources[key], tokens[key] = [proof.dev, proof.ino], proof.token
        else:
            identity = relocate._identity(source)  # noqa: SLF001 - same package
            if identity is not None:
                sources[key] = list(identity)
    return dataclasses.replace(move, sources=sources, tokens=tokens)


def run(
    move: ImportMove,
    *,
    cancel: Cancel | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[relocate.Moved]:
    """Move every folder; on any failure put the ones already moved back, then re-raise."""
    move = _stamp_sources(move)
    try:
        _write_journal(move, move.copies, claim=True)
    except OSError as exc:
        raise relocate.RelocateError(
            f"Could not record the move before starting it: {exc}"
        ) from exc
    done: list[tuple[Path, Path, relocate.Moved]] = []
    results: list[relocate.Moved] = []
    copies: dict[str, list[int]] = {}
    try:
        for key, source, destination in move.trees:
            if key in move.done:
                # A killed earlier run already finished this tree; finish the rest.
                results.append(relocate.Moved(renamed=True, kept_old=None))
                continue
            result = relocate.move_tree(source, destination, cancel=cancel, progress=progress)
            done.append((source, destination, result))
            results.append(result)
            if result.kept_old is not None:
                identity = relocate._identity(destination)  # noqa: SLF001 - same package
                if identity is not None:
                    copies[key] = list(identity)
                    _write_journal(move, {**move.copies, **copies})
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


def _already_recorded(profile: dict, journal: dict) -> bool:
    """Whether the profile already uses every folder the journal moved (the move finished).

    Strict on purpose: each tree's destination must be inside the journal's game folder, be
    an existing folder (not a link) and be exactly where the profile points; a game folder
    inside the prefix must match too. Anything less is left to :func:`_recover_move`.
    """
    home_text = journal.get("home")
    trees = journal.get("trees")
    if not isinstance(home_text, str) or not home_text or not Path(home_text).is_absolute():
        return False
    if not isinstance(trees, list) or not trees:
        return False
    home = Path(home_text)
    for item in trees:
        if not isinstance(item, dict):
            return False
        key, destination_text = item.get("key"), item.get("destination")
        if key not in ("install_dir", "prefix", "wineprefix"):
            return False
        if not isinstance(destination_text, str) or not destination_text:
            return False
        destination = Path(destination_text)
        if not destination.is_absolute() or relink._under(destination, home) is None:  # noqa: SLF001
            return False
        if destination.is_symlink() or not destination.is_dir():
            return False
        current = str(profile.get(key) or "")
        if not current or not _same_path(Path(current).expanduser(), destination):
            return False
    inside = journal.get("install_inside")
    if inside is not None:
        if not isinstance(inside, str) or not inside:
            return False
        current = str(profile.get("install_dir") or "")
        if not current or not _same_path(Path(current).expanduser(), Path(inside)):
            return False
    return True


def finish_interrupted(config) -> list[str]:
    """Settle an import-move a crash interrupted (#1372 phase 3); notes, the caller saves.

    Every tree in the library: the move finished and only the record was missing, so it is
    recorded (:func:`commit`). Otherwise each folder that was renamed into the library is
    renamed back to where the profile still points (never deleted), so the game is as it was
    before the move. A journal :func:`_recover_move` does not accept (another game, a changed
    profile, a path outside its game folder, damage), or a folder in the way, is left alone
    and the journal kept for a later look or a retry from Settings.
    """
    path = _journal_path(config)
    if not os.path.lexists(path):
        return []
    kept = f"its record {path} was kept and nothing was moved."
    journal = _read_journal(config)
    if journal is None:
        return [f"An interrupted move into a library could not be read; {kept}"]
    name, library = journal.get("profile"), journal.get("library")
    if not isinstance(name, str) or name not in config.profiles:
        return [f"An interrupted move into a library is for a game that is gone; {kept}"]
    if not isinstance(library, str) or not library:
        return [f"{name}: an interrupted move into a library names no library; {kept}"]
    if _already_recorded(config.profiles[name], journal):
        # b24 (and next before 0946cc89) left the journal of a SUCCESSFUL move behind. The
        # game already uses the new folders: only the record goes, nothing on disk moves.
        try:
            path.unlink()
        except OSError:
            return [f"{name}: could not remove the record of a finished move ({path})"]
        return [f"{name}: removed the record of a move into a library that had finished"]
    try:
        move = _recover_move(config, name, library)
    except Unverified as exc:
        return [f"{name}: {exc}; {kept}"]
    except (OSError, ValueError):
        move = None
    if move is None:
        return [f"{name}: an interrupted move into a library no longer matches the game; {kept}"]
    keys = [key for key, _, _ in move.trees]
    if set(move.done) == set(keys):
        # Recorded in memory only: the journal stays until the caller has SAVED the new
        # places (#1486); forget_if_recorded() removes it then, or the next start does.
        _record(config, name, move)
        return [f"{name}: finished its interrupted move into {move.home}"]
    back: list[tuple[Path, Path]] = []
    copies: list[Path] = []
    for key, source, destination in move.trees:
        if key not in move.done:
            continue
        if os.path.lexists(source):
            # Copied to another disk: the original is still there. Only a copy that is still
            # the very folder the run made goes (#1400); anything else is kept.
            recorded = move.copies.get(key)
            if source.is_dir() and folderproof.matches(
                destination, "import", folderproof.record_of(recorded, move.tokens.get(key))
            ):
                copies.append(destination)
                continue
            return [f"{name}: {source} is in the way of putting the game back; {kept}"]
        back.append((destination, source))
    for destination in copies:
        outcome = removal._rmtree_safely(destination)  # noqa: SLF001 - Nadare's own copy
        if outcome.mounts or outcome.leftovers:
            return [f"{name}: could not remove the unfinished copy {destination}; {kept}"]
    for destination, source in back:
        try:
            os.rename(destination, source)
        except OSError as exc:
            return [f"{name}: could not put {destination} back ({exc}); {kept}"]
    _remove_if_empty(move.home)
    _clear_journal(move)
    if not back and not copies:
        return [f"{name}: an interrupted move into a library had not moved anything yet"]
    if copies and not back:
        return [f"{name}: removed the unfinished copy an interrupted move into a library made"]
    return [f"{name}: put back the folders an interrupted move into a library had moved"
            + (" and removed its unfinished copy" if copies else "")]


def clear_journal(move: ImportMove) -> None:
    """Forget a finished import move once the profile records the new places."""
    _clear_journal(move)


def _record(config, name: str, move: ImportMove) -> None:
    relink.apply(config.profiles[name], relink_plan(config.profiles[name], move))
    libraries.sync_game(config, name, remember_library=True)


def commit(config, name: str, move: ImportMove) -> None:
    """Record the new places, write the manifest and list the library. The caller saves.

    Clears the journal: only for a caller that saves right after and can redo the move on a
    failed save. Start-up recovery keeps it until :func:`forget_if_recorded` (#1486).
    """
    _record(config, name, move)
    _clear_journal(move)


def _saved_profile(config, name: str) -> dict | None:
    """``name``'s profile as the config FILE has it (read only; never what is in memory)."""
    try:
        raw = json.loads(Path(config.path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    return profile if isinstance(profile, dict) else None


def forget_if_recorded(config) -> bool:
    """Remove the journal once the SAVED config uses every folder it moved (#1486)."""
    path = _journal_path(config)
    journal = _read_journal(config)
    if journal is None:
        return False
    name = journal.get("profile")
    profile = _saved_profile(config, name) if isinstance(name, str) else None
    if profile is None or not _already_recorded(profile, journal):
        return False
    try:
        path.unlink()
    except OSError:
        log.debug("could not clear import-move journal %s", path)
        return False
    return True


__all__ = [
    "ImportMove", "Unverified", "commit", "finish_interrupted", "forget_if_recorded",
    "movable", "plan", "relink_plan", "run",
]
