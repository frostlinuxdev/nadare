# SPDX-License-Identifier: GPL-3.0-or-later
"""Point a profile at a Wine prefix the user moved by hand.

Nothing is copied, moved or deleted: the plan only works out which profile fields change,
after checking the new place really looks like the prefix.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import flatpakfs, location, paths, prefixguard
from .libraries import MANIFEST_NAME, MANIFEST_TMP_NAME, default_root, game_folder_name
from .shadercache import VERSIONS_FILE


class RelinkError(ValueError):
    """The new place cannot be used; the message says why."""


@dataclass(frozen=True)
class Relink:
    changes: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    #: Things the user should read before agreeing (a shared prefix, a game folder left behind).
    warnings: list[str] = field(default_factory=list)
    #: A game folder that sits next to the new prefix, offered but never applied on its own.
    sibling_install: str | None = None


def _has_drive_c(path: Path) -> bool:
    return (path / "drive_c").is_dir() or (path / "pfx" / "drive_c").is_dir()


def _under(path: Path, root: Path) -> Path | None:
    """``path`` relative to ``root``, also when one of them is spelled through a symlink."""
    try:
        return path.relative_to(root)
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return None


def _setting(profile: Mapping[str, Any], key: str) -> Any:
    """A profile key from the top level or its ``extra`` mapping, as removal reads it."""
    value = profile.get(key)
    if value not in (None, ""):
        return value
    extra = profile.get("extra")
    return extra.get(key) if isinstance(extra, dict) else None


def _absolute(path: str | Path) -> Path:
    """``path`` with ``~`` expanded and made absolute without changing where it points.

    Lexically collapsing ``link/..`` would land somewhere other than the kernel does, so a path
    that holds ``..`` is resolved physically; any other spelling (symlinks included) is kept.
    """
    text = os.path.expanduser(str(path))
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    if ".." in Path(text).parts:
        return Path(os.path.realpath(text))
    return Path(os.path.normpath(text))


def is_inside(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or lies under it, also through a symlink spelling."""
    return _under(path, root) is not None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.absolute() == b.absolute() or a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return a.absolute() == b.absolute()


def _canonical(path: Path) -> Path:
    """``path`` with its parents resolved but not itself, so a link keeps its own name."""
    return path.parent.resolve() / path.name


def _link_inside_own_folders(literal: Path, profile: Mapping[str, Any]) -> bool:
    """True when a link sits in the prefix's path at or below a folder that is Nadare's own.

    Nadare's own: anything below ``profiles/`` and any folder recorded in ``owned_dirs``. A path
    that cannot be checked counts as linked, so the move is refused rather than guessed.
    """
    try:
        roots = [paths.profiles_dir().resolve()]
        dirs = profile.get("owned_dirs")
        for entry in dirs if isinstance(dirs, list) else ():
            if isinstance(entry, str) and entry:
                roots.append(_canonical(Path(os.path.abspath(os.path.expanduser(entry)))))
        for part in (literal, *literal.parents):
            if not part.is_symlink():
                continue
            if part == literal:
                return True
            spot = _canonical(part)
            for root in roots:
                if spot != roots[0] and (spot == root or root in spot.parents):
                    return True
    except (OSError, RuntimeError):
        return True
    return False


def check_movable(
    profile: dict[str, Any],
    *,
    name: str | None = None,
    profiles: Mapping[str, Any] | None = None,
) -> None:
    """Raise :class:`RelinkError` unless the profile's prefix can be moved and relinked afterwards.

    With ``name`` and ``profiles`` a prefix another profile also uses is refused: only the
    selected profile is relinked afterwards, so the other would be left pointing nowhere (#971).

    Called before anything is moved, so a move never ends in a profile Nadare cannot update.
    """
    text = str(location._setting(profile, "prefix") or "")
    if not text:
        raise RelinkError("This game has no prefix set, so there is nothing to move.")
    literal = Path(os.path.abspath(os.path.expanduser(text)))
    if _link_inside_own_folders(literal, profile):
        # A link inside Nadare's own folders (profiles/<name> linked to Documents, say) would move
        # the folder it points to, out of the player's own place (storage simulator, 2026-11-14).
        # A link above them (~/Games on another disk, /home -> /var/home) is fine: renaming
        # through it still moves the real game folder (review of #1389).
        raise RelinkError(
            f"{literal} is reached through a link. Nadare only moves real folders: move the "
            'folder it points to yourself, then use "I moved it myself".'
        )
    old = Path(text).expanduser()
    if old.is_symlink():
        raise RelinkError(
            f"{old} is a link to another folder. Nadare only moves real folders: move the "
            "folder it points to yourself, then use \"I moved it myself\"."
        )
    if not old.is_dir() or not _has_drive_c(old):
        raise RelinkError(
            f"{old} does not look like a Wine prefix (no drive_c folder), so Nadare will not "
            "move it."
        )
    if profiles and managed_home(profile) is None:
        who = _other_inside(profiles, name, old)
        if who:
            raise RelinkError(
                f"{who} also uses this prefix, and only one game can be relinked after a move. "
                f"Move or remove {who} first."
            )
    install = str(location._setting(profile, "install_dir") or "")
    if install:
        inside = _under(Path(install).expanduser(), old)
        if inside is not None and not (old / inside).is_dir():
            raise RelinkError(
                f"The game folder {old / inside} was not found inside the prefix, so Nadare "
                "will not move it."
            )


def _tree_values(profile: Mapping[str, Any]) -> list[str]:
    """Every folder a profile records: its owned folders, game folder and prefix."""
    recorded = [
        *(profile.get("owned_dirs") or ()),
        _setting(profile, "install_dir"),
        _setting(profile, "prefix"),
        _setting(profile, "wineprefix"),
    ]
    return [value for value in recorded if isinstance(value, str) and value]


def _claimed(profiles: Mapping[str, Any], folder: Path) -> bool:
    """True when a game already uses ``folder`` or something inside it (#1372 step 2)."""
    return (
        location.overlapping(
            profiles, folder, excluding=None, direction="contains", ignore_broad=False
        )
        is not None
    )


def _other_inside(profiles: Mapping[str, Any], name: str | None, root: Path) -> str | None:
    """The title of another game that keeps something inside ``root``, if any."""
    claim = location.overlapping(
        profiles, root, excluding=name, direction="contains", ignore_broad=False
    )
    return claim.title if claim is not None else None


def _inside_other(profiles: Mapping[str, Any], name: str | None, path: Path) -> str | None:
    """The title of another game whose folders ``path`` lies inside, if any."""
    claim = location.overlapping(
        profiles, path, excluding=name, direction="inside", ignore_broad=False
    )
    return claim.title if claim is not None else None


def _is_profile_dir(home: Path) -> bool:
    """True for ``<data>/profiles/<id>``, the folder older Nadare kept a game in."""
    try:
        return _same(home.parent, paths.profiles_dir())
    except OSError:
        return False


#: Nadare's own records about a move in progress or still to finish: they name the old place
#: on purpose and are not settings the game reads.
_BOOKKEEPING_KEYS = frozenset(
    {
        "move_in_progress",
        "move_originals_pending",
        "removal_pending",
        "moved_by_nadare",
        "location",
    }
)

#: Profile keys ``plan_home_move`` re-roots when the folder moves.
_REROOTED_KEYS = frozenset(
    {"prefix", "install_dir", "installed_prefix", "prefix_backups", "owned_dirs", "owned_paths"}
)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _profile_dir_moves_whole(profile: Mapping[str, Any], home: Path, *, exists: bool) -> bool:
    """True when ``<data>/profiles/<id>`` holds nothing but the game and its prefix.

    The backends keep their own state (sign-in, stubs, caches) in that folder, found by the game's
    name, so moving it whole would strand that state in the library. Paths the profile keeps
    elsewhere (an absolute exe, a script, an ``extra`` setting) inside it would be left pointing at
    the old place. Either keeps the game on the prefix-only move.
    """
    if exists:
        try:
            # Nadare's own manifest (b22 folder rule) holds only paths relative to the folder.
            # The runner versions of the last Play (#1364) are Nadare's too: every game that
            # was played has them, and losing them only means the next Play records them again.
            allowed = {"prefix", "install", MANIFEST_NAME, MANIFEST_TMP_NAME, VERSIONS_FILE}
            if any(entry.name not in allowed for entry in home.iterdir()):
                return False
        except OSError:
            return False
    for key, value in profile.items():
        if key in _REROOTED_KEYS or key in _BOOKKEEPING_KEYS:
            continue
        for text in _strings(value):
            if text.startswith(("/", "~")) and _under(_absolute(text), home) is not None:
                return False
    return True


def _legacy_dirs(owned: list[Any], prefix: str, install: str) -> list[str]:
    """The home of a profile older Nadare made, which recorded no ``owned_dirs``.

    Add Game always made ``<library>/Nadare/<Game>/{prefix,install}``, and the ownership record
    says Nadare made both trees. A prefix and game folder that still look exactly like that are
    taken to be that layout; anything else is left to the user.
    """
    home = Path(prefix).expanduser().parent
    if Path(prefix).name != "prefix" or not (
        home.parent.name == "Nadare" or _is_profile_dir(home)
    ):
        return []
    if install and (Path(install).expanduser() != home / "install" or "install_dir" not in owned):
        return []
    return [str(home)]


def managed_home(profile: dict[str, Any], *, exists: bool = True) -> Path | None:
    """The ``Nadare/<Game>`` folder Add Game made for this profile, if its layout is still intact.

    The prefix (and the game folder, when there is one) must both sit inside a folder recorded in
    ``owned_dirs``, and the prefix must be one Nadare made. Anything else was put there by the
    user, so only its prefix can move. Before a move the prefix and game folder must both exist.
    ``exists=False`` is for after a move, when the old folder is gone and only the records are
    left to read.
    """
    dirs = profile.get("owned_dirs")
    owned = profile.get("owned_trees")
    prefix = str(location._setting(profile, "prefix") or "")
    if not isinstance(owned, list) or "prefix" not in owned or not prefix:
        return None
    install = str(location._setting(profile, "install_dir") or "")
    if not isinstance(dirs, list) or not dirs:
        dirs = _legacy_dirs(owned, prefix, install)
    for entry in dirs:
        home = Path(str(entry)).expanduser()
        if not home.name or home.is_symlink() or (exists and not home.is_dir()):
            continue
        if _under(Path(prefix).expanduser(), home) is None:
            continue
        if install and _under(Path(install).expanduser(), home) is None:
            continue
        # A game kept in Nadare's own profiles folder may not have its game folder yet (a launcher
        # installs it later); a Nadare/<Game> layout must have both.
        waits = _is_profile_dir(home)
        if waits and not _profile_dir_moves_whole(profile, home, exists=exists):
            continue
        if exists and not (
            Path(prefix).expanduser().is_dir()
            and (not install or waits or Path(install).expanduser().is_dir())
        ):
            continue  # a half-missing layout cannot move as one unit: re-rooting would fail
        if not _is_game_folder(profile, home):
            continue  # only the folder location calls the game's own moves whole (#1372 step 4)
        return home
    return None


def _is_game_folder(profile: Mapping[str, Any], home: Path) -> bool:
    """Whether ``home`` is the folder :func:`location.game_folder` names for this profile.

    The game's own folder has one answer (``location``); a whole-folder Move never takes a
    folder that answer does not name (phase 1 step 4 of #1372). ``location`` models catalog
    games only: a custom Wine game Add Game laid out keeps the ownership checks above alone,
    as Uninstall does for it.
    """
    if not location.catalog_kind(profile.get("backend")):
        return True
    name = home.name if _is_profile_dir(home) else ""
    folder = location.game_folder(profile, name)
    return folder is not None and _same(folder, home)


def home_name(profile: Mapping[str, Any], home: Path) -> str:
    """The folder name a game gets in a library: its own for ``Nadare/<Game>``, else its title.

    A game kept in Nadare's profiles folder is named by an id (``nikke-goddess-of-victory``); in
    a library it takes its title, as Add Game would name it. A title cannot reach outside the
    library, so path separators are dropped.
    """
    if home.parent.name == "Nadare" or not _is_profile_dir(home):
        return home.name
    title = str(profile.get("title") or "")
    name = game_folder_name(title)
    return home.name if name == "game" and not title.strip(" ./\\:*?\"<>|") else name


def prefix_destination(profile: Mapping[str, Any], old: Path, parent: str | Path) -> Path:
    """Where a prefix-only move puts the prefix inside the folder the user picked.

    A folder just called ``prefix`` says nothing about the game, and two games moved into the same
    folder would collide, so it is named ``<Title> prefix``. A folder the user named is kept.
    Taken names get `` 2``, `` 3``.
    """
    base = Path(parent).expanduser()
    name = old.name
    if name == "prefix":
        title = str(profile.get("title") or "").replace("/", " ").replace("\\", " ")
        title = " ".join(title.strip(" .").split())
        name = f"{title} prefix" if title else name
    candidate = base / name
    index = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = base / f"{name} {index}"
        index += 1
    return candidate


def free_folder(root: str | Path, name: str, profiles: Mapping[str, Any]) -> Path:
    """``root/name``, or ``... 2``, ``... 3``: the first neither on disk nor claimed.

    The one rule Add Game and Move use. A dangling symlink is not free: creating through it
    would write outside the library, and a profile may already record the path.
    """
    base = _absolute(root)
    candidate = base / name
    index = 2
    while (
        (candidate.exists() and not _stale_folder(candidate, profiles))
        or candidate.is_symlink()
        or os.path.lexists(candidate.with_name(candidate.name + ".nadare-partial"))
        or _claimed(profiles, candidate)
    ):
        candidate = base / f"{name} {index}"
        index += 1
    return candidate


def _stale_folder(folder: Path, profiles: Mapping[str, Any]) -> bool:
    """A game folder holding nothing but Nadare's own manifest (#1362): free to use again.

    A game added but never installed (or uninstalled by an older Nadare) left
    ``<library>/Nadare/<Game>/nadare-game.json`` alone; a reinstall then went to
    "<Game> 2". Only an unclaimed real folder whose every entry is the manifest or its temp
    file counts. Checking changes nothing; :func:`clear_stale_folder` clears it once the
    player confirmed (storage review A9).
    """
    ours = {MANIFEST_NAME, MANIFEST_TMP_NAME}
    try:
        if folder.is_symlink() or not folder.is_dir() or _claimed(profiles, folder):
            return False
        entries = list(folder.iterdir())
        return bool(entries) and all(
            e.name in ours and e.is_file() and not e.is_symlink() for e in entries
        )
    except OSError:
        return False


def clear_stale_folder(folder: Path, profiles: Mapping[str, Any]) -> bool:
    """Remove a :func:`_stale_folder` so a move can create it; ``True`` when it went."""
    folder = Path(folder)
    if not _stale_folder(folder, profiles):
        return False
    try:
        for entry in list(folder.iterdir()):
            entry.unlink()
        folder.rmdir()
    except OSError:
        return False
    return True


def free_home(library: str | Path, name: str, profiles: Mapping[str, Any]) -> Path:
    """``<library>/Nadare/<name>``, or ``... 2``, ``... 3``: the first neither on disk nor claimed.

    The same naming Add Game uses, so a game moved into a library lands where a new one would.
    """
    return free_folder(_absolute(library) / "Nadare", name, profiles)


def check_place(path: str | Path, *, library: bool = False) -> None:
    """Refuse a place the shared rule or the Flatpak forbids (#1229); every path-taking flow.

    ``library``: ``path`` is a library (the top of a drive is fine there).
    """
    target = _absolute(path)
    reason = prefixguard.place_reason(target, allow_drive_top=library)
    if reason:
        raise RelinkError(f"{target} is {reason}. Pick another folder.")
    out_of_reach = flatpakfs.unreachable_reason(target)
    if out_of_reach:
        raise RelinkError(out_of_reach)


def check_library_target(library: str | Path) -> None:
    """Refuse Nadare's own folder as the library a game moves or is planned into.

    The default library keeps a game as ``profiles/<name>``; ``free_home`` would make
    ``profiles/Nadare/<Game>`` there, a layout Find games never reads (disjoint audit W1-1).
    """
    if _same(_absolute(library), default_root()):
        raise RelinkError(
            "That is Nadare's own folder for games; it cannot take a moved game. "
            "Pick another library."
        )


def check_home_target(
    profile: dict[str, Any],
    library: str | Path,
    *,
    name: str | None = None,
    profiles: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a library that is where the game already is, inside its own folder, or tangled up
    with another game (one that keeps files inside this game's folder, or a library that lies
    inside another game's files). ``name`` and ``profiles`` enable the cross-game checks."""
    home = managed_home(profile)
    if home is None:
        raise RelinkError("This game's folders were not laid out by Nadare.")
    chosen = _absolute(library)
    check_library_target(chosen)
    check_place(chosen, library=True)
    if _same(chosen / "Nadare" / home_name(profile, home), home):
        raise RelinkError("The game is already there. Pick a different folder.")
    if _under(chosen, home) is not None:
        raise RelinkError("That folder is inside the game's own folder. Pick another one.")
    if profiles:
        who = _other_inside(profiles, name, home)
        if who:
            raise RelinkError(
                f"{who} keeps its files inside this game's folder, so it would be moved "
                f"along. Move or remove {who} first."
            )
        who = _inside_other(profiles, name, chosen)
        if who:
            raise RelinkError(f"That folder is inside {who}'s files. Pick another one.")


def plan_home_move(
    profile: dict[str, Any], new_home: str | Path, *, install_existed: bool = True
) -> Relink:
    """What changes after Nadare moved the whole ``Nadare/<Game>`` folder to ``new_home``.

    Every path under the old folder is re-rooted, the ownership records follow (Nadare made the
    move, so the new place is Nadare's), and ``owned_dirs`` names the new folder.
    """
    home = managed_home(profile, exists=False)
    if home is None:
        raise RelinkError(
            "This game's folders were not laid out by Nadare, so they cannot move together."
        )
    new = _absolute(new_home)
    changes: dict[str, Any] = {}
    notes: list[str] = []
    for key in ("prefix", "install_dir", "installed_prefix"):
        value = str(location._setting(profile, key) or "")
        if not value:
            continue
        inside = _under(Path(value).expanduser(), home)
        if inside is not None:
            changes[key] = str(new / inside)
    prefix = Path(changes["prefix"])
    if not prefix.is_dir() or not _has_drive_c(prefix):
        raise RelinkError(f"The prefix did not arrive at the new place ({prefix}).")
    install = changes.get("install_dir")
    if install and install_existed and not Path(install).is_dir():
        raise RelinkError(f"The game folder did not arrive at the new place ({install}).")
    notes.append(f"Wine prefix: {prefix}")
    if install:
        notes.append(f"Game folder: {install}")
    dirs = profile.get("owned_dirs")
    changes["owned_dirs"] = [
        str(new) if _same(Path(str(entry)).expanduser(), home) else entry
        for entry in (dirs if isinstance(dirs, list) else [])
    ]
    if str(new) not in changes["owned_dirs"]:
        changes["owned_dirs"].append(str(new))  # a legacy profile learns its home on the move
    owned = profile.get("owned_trees")
    recorded = profile.get("owned_paths")
    paths = dict(recorded) if isinstance(recorded, dict) else {}
    for key in ("install_dir", "prefix"):
        if isinstance(owned, list) and key in owned and key in changes:
            paths[key] = str(Path(changes[key]).resolve())
    changes["owned_paths"] = paths
    backups = profile.get("prefix_backups")
    if isinstance(backups, list):
        # Old prefixes a rebuild parked next to the prefix travel with the folder.
        changes["prefix_backups"] = [
            str(new / inside)
            if isinstance(entry, str)
            and (inside := _under(Path(entry).expanduser(), home)) is not None
            else entry
            for entry in backups
        ]
    return Relink(changes=changes, notes=notes)


def _follow_backup(entry: Any, old_dir: Path, new_dir: Path) -> Any:
    if not isinstance(entry, str):
        return entry
    path = Path(entry).expanduser()
    inside = _under(path, old_dir)
    if inside is None or path.exists():
        return entry
    moved = new_dir / inside
    return str(moved) if moved.is_dir() else entry


#: ``{key: {"path": resolved path, "id": [st_dev, st_ino]}}`` for folders Nadare itself moved
#: outside the game's folder (a prefix-only Move). Removal accepts such a folder only while path
#: and identity both still match (#1366 A3).
MOVED_BY_NADARE = "moved_by_nadare"


def _folder_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def moved_by_nadare(profile: Mapping[str, Any], path: Path) -> bool:
    """Whether ``path`` is a folder Nadare itself moved there for this profile (path + inode)."""
    proof = profile.get(MOVED_BY_NADARE)
    if not isinstance(proof, dict):
        return False
    try:
        resolved = str(path.resolve())
    except (OSError, RuntimeError):
        return False
    identity = _folder_identity(path)
    if identity is None or path.is_symlink():
        return False
    for entry in proof.values():
        if (
            isinstance(entry, dict)
            and entry.get("path") == resolved
            and entry.get("id") == list(identity)
        ):
            return True
    return False


def plan_relink(
    profile: dict[str, Any],
    new_prefix: str | Path,
    *,
    made_by_nadare: bool = False,
    name: str | None = None,
    profiles: Mapping[str, Any] | None = None,
) -> Relink:
    """What changes if ``profile``'s prefix now lives at ``new_prefix``.

    ``made_by_nadare`` is set after Nadare itself moved or copied the folder, so the new
    place is Nadare's even though the old one may still exist. A hand relink never passes
    that on: Nadare only deletes folders it made, and it cannot tell that the folder the
    user picked is the one it made. ``name`` and ``profiles`` let the plan notice a prefix
    another game already uses.
    """
    new = _absolute(new_prefix)
    check_place(new)
    if not new.is_dir() or not _has_drive_c(new):
        raise RelinkError(
            f"{new} does not look like a Wine prefix (no drive_c folder). "
            "Pick the folder you moved."
        )
    old_text = str(_setting(profile, "prefix") or "")
    old = _absolute(old_text) if old_text else None
    if old is not None and _same(old, new):
        raise RelinkError("The game already uses that prefix.")

    changes: dict[str, Any] = {"prefix": str(new)}
    notes: list[str] = []
    warnings: list[str] = []
    owned = profile.get("owned_trees")
    recorded = profile.get("owned_paths")
    moved_keys: list[str] = []
    if isinstance(owned, list) and "prefix" in owned:
        moved_keys.append("prefix")

    sibling: str | None = None
    install = str(location._setting(profile, "install_dir") or "")
    if install and old is not None:
        install_path = Path(install).expanduser()
        inside = _under(install_path, old)
        if inside is not None:
            rerooted = new / inside
            if not rerooted.is_dir():
                raise RelinkError(
                    f"The game folder is not at the new place ({rerooted}). "
                    "Move the whole prefix, including the game folder."
                )
            changes["install_dir"] = str(rerooted)
            notes.append(f"Game folder: {rerooted}")
            if isinstance(owned, list) and "install_dir" in owned:
                moved_keys.append("install_dir")
        else:
            if install_path.is_dir():
                warnings.append(
                    f"The game folder stays at {install} (it is not inside the prefix)."
                )
            else:
                warnings.append(
                    f"The game folder {install} is not there any more and is not inside "
                    "the prefix, so Nadare cannot follow it."
                )
            if install_path.parent == old.parent:  # the Add Game layout: install next to prefix
                candidate = new.parent / install_path.name
                if candidate.is_dir() and not _same(candidate, install_path):
                    sibling = str(candidate)

    backups = profile.get("prefix_backups")
    if isinstance(backups, list) and old is not None:
        # The user moved the whole game folder: a backup a rebuild parked beside the old prefix
        # went with it. Follow only entries that are gone from the old place and found in the new.
        moved = [_follow_backup(entry, old.parent, new.parent) for entry in backups]
        if moved != backups:
            changes["prefix_backups"] = moved

    installed = profile.get("installed_prefix")
    if (
        made_by_nadare
        and installed
        and old is not None
        and _same(Path(str(installed)).expanduser(), old)
    ):
        # The backend's uninstall guard compares this. Only Nadare's own move proves the new
        # folder is the one it installed into; a hand-picked folder never inherits it.
        changes["installed_prefix"] = str(new)

    if moved_keys:
        paths = dict(recorded) if isinstance(recorded, dict) else {}
        proof = profile.get(MOVED_BY_NADARE)
        proof = dict(proof) if isinstance(proof, dict) else {}
        if made_by_nadare:
            for key in moved_keys:
                paths[key] = str(Path(changes[key]).resolve())
                # The folder's identity too: a hand-edited owned_paths cannot fake it, so
                # Uninstall can tell Nadare's own Move from an edit (#1366 A3, #1229).
                identity = _folder_identity(Path(changes[key]))
                if identity is None:
                    proof.pop(key, None)
                else:
                    proof[key] = {"path": paths[key], "id": list(identity)}
        else:
            for key in moved_keys:
                paths.pop(key, None)
                proof.pop(key, None)
            changes["owned_trees"] = [key for key in owned if key not in moved_keys]
        changes["owned_paths"] = paths
        if proof or MOVED_BY_NADARE in profile:
            changes[MOVED_BY_NADARE] = proof
    if not made_by_nadare and isinstance(owned, list) and "prefix" in owned:
        notes.append(
            "Because you moved it yourself, Nadare will not delete this folder when you "
            "remove the game."
        )

    if profiles and name is not None:
        for other_name, other in profiles.items():
            if other_name == name or not isinstance(other, dict):
                continue
            theirs = str(_setting(other, "prefix") or _setting(other, "wineprefix") or "")
            if theirs and _same(Path(theirs).expanduser(), new):
                warnings.append(
                    f"This folder is also used by {other.get('title') or other_name}. "
                    "The two games would share one Wine prefix."
                )
    return Relink(changes=changes, notes=notes, warnings=warnings, sibling_install=sibling)


def follow_install(profile: dict[str, Any], plan: Relink, install_dir: str | Path) -> Relink:
    """``plan`` plus a game folder the user agreed to use (the sibling the plan offered).

    Like the prefix, a folder moved by hand is no longer one Nadare knows it made.
    """
    changes = dict(plan.changes)
    changes["install_dir"] = str(Path(install_dir))
    owned = changes.get("owned_trees", profile.get("owned_trees"))
    if isinstance(owned, list) and "install_dir" in owned:
        changes["owned_trees"] = [key for key in owned if key != "install_dir"]
        paths = changes.get("owned_paths", profile.get("owned_paths"))
        changes["owned_paths"] = {
            key: value for key, value in (paths or {}).items() if key != "install_dir"
        }
    return replace(plan, changes=changes, sibling_install=None)


def apply(profile: dict[str, Any], plan: Relink) -> None:
    """Write ``plan`` into ``profile`` (the caller saves the config under its lock)."""
    profile.update(plan.changes)
