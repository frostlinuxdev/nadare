# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where a game lives: the one answer (#1372, storage phase 1).

Nadare grew several records and several functions that each answered "which folder is this
game's own?" a little differently (see #1372 and ``tests/test_storage_shapes.py``). This
module is the answer; the older helpers call it. Nothing here writes to disk or to the config.

A catalog game's *own folder* is the folder Nadare made for it:

1. its one ``owned_dirs`` entry, a ``<library>/Nadare/<Game>`` folder;
2. the same layout from before ``owned_dirs`` existed (``<library>/Nadare/<Game>/{prefix,
   install}`` with an ownership record for those trees);
3. a default-library folder ``profiles/<x>`` recorded through ``owned_paths`` (an older Add Game
   let the profile name differ from the folder name);
4. this profile's own ``profiles/<name>``, when the game keeps everything there (or names no
   folder: the backend's default prefix is there), or when a folder Nadare made is there.

Anything else is the player's: an imported game, a prefix they picked, a Steam game. Those
have no own folder (``None``), so nothing derived from "Nadare's folder" (manifests, Uninstall's
folder tidying, "made by Nadare" in Settings) ever applies to them.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import directkinds, paths
from .paths import real as _real

NADARE_DIR = "Nadare"
#: The profile keys that name a game's folders.
FOLDER_KEYS = ("install_dir", "prefix")


def _text(value: Any) -> str | None:
    """A usable path string, or ``None`` (empty, not text, or a hand-edited NUL)."""
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        return value
    return None


def _setting(profile: Mapping[str, Any], key: str) -> str | None:
    """A folder key from the profile's top level or its ``extra`` mapping, as the backends
    read it (a folder set only under ``extra`` is still used by that game)."""
    value = _text(profile.get(key))
    if value is not None:
        return value
    extra = profile.get("extra")
    return _text(extra.get(key)) if isinstance(extra, Mapping) else None


def catalog_kind(kind: object) -> bool:
    """One profile per game: the kinds Nadare lays out itself (not generic Wine/Steam)."""
    return directkinds.storage_class(kind) == "catalog"


def _legacy_library_folder(profile: Mapping[str, Any]) -> Path | None:
    """``<library>/Nadare/<Game>`` of a profile made before ``owned_dirs`` existed.

    Add Game always made ``<Game>/{prefix,install}`` there and recorded that it made the trees,
    just not the folder (before 2026-10-27). Only that exact shape, with that record, counts.
    """
    owned = profile.get("owned_trees")
    prefix = _setting(profile, "prefix")
    if not isinstance(owned, list) or "prefix" not in owned or prefix is None:
        return None
    prefix_path = Path(os.path.expanduser(prefix))
    home = prefix_path.parent
    if prefix_path.name != "prefix" or home.parent.name != NADARE_DIR:
        return None
    install = _setting(profile, "install_dir")
    if install is not None:
        if "install_dir" not in owned or Path(os.path.expanduser(install)) != home / "install":
            return None
    return home


def _recorded_default_folder(profile: Mapping[str, Any], root: Path) -> Path | None:
    """A default-library game's real folder from the ``install``/``prefix`` it owns."""
    owned = profile.get("owned_trees")
    if not isinstance(owned, list):
        return None
    homes: set[Path] = set()
    for key, leaf in (("install_dir", "install"), ("prefix", "prefix")):
        value = _setting(profile, key)
        if value is None:
            continue
        if key not in owned:
            return None
        path = _real(value)
        if path.name != leaf:
            return None
        homes.add(path.parent)
    if len(homes) != 1:
        return None
    home = homes.pop()
    return home if home.parent == root else None


def _keeps_its_game_in(profile: Mapping[str, Any], folder: Path) -> bool:
    """Whether ``folder`` is where this profile keeps its game.

    True when every folder the profile names lies inside it (none named: the backend's own
    default prefix is there), or when a folder Nadare owns (``owned_trees``) lies inside it:
    a player's own prefix elsewhere does not make Nadare's install folder theirs.
    """
    real = _real(folder)
    owned = profile.get("owned_trees")
    owned = owned if isinstance(owned, list) else []
    every, some_owned = True, False
    for key in FOLDER_KEYS:
        value = _setting(profile, key)
        if value is None:
            continue
        target = _real(value)
        inside = target == real or real in target.parents
        every = every and inside
        some_owned = some_owned or (inside and key in owned)
    return every or some_owned


def game_folder(
    profile: Mapping[str, Any],
    name: str,
    default_root: str | os.PathLike[str] | None = None,
) -> Path | None:
    """The folder Nadare made for this catalog game, or ``None`` (the player's, or generic).

    ``default_root`` is the default library (``profiles``) for a caller that plans a game
    before Nadare's own folders exist; normally it is :func:`paths.profiles_dir`.

    A current stored record (#1372 phase 2b) is the answer. A stale, missing or damaged one
    falls back to the legacy keys, and so does a folder in the default library: that answer
    depends on the disk (a link put at ``profiles/<name>`` never counts, B1), so it is
    worked out again each time.
    """
    if not catalog_kind(profile.get("backend")):
        return None
    if default_root is None and record_is_current(profile):
        found, folder = _recorded_folder(profile)
        if found:
            return folder
    return _legacy_game_folder(profile, name, default_root)


def _recorded_folder(profile: Mapping[str, Any]) -> tuple[bool, Path | None]:
    """``(True, answer)`` when the stored record settles it, else ``(False, None)``."""
    record = profile.get(RECORD_KEY)
    folder = record.get("folder") if isinstance(record, dict) else None
    if folder is None:
        return True, None  # stored as "not Nadare's": the safe direction
    if not isinstance(folder, str) or not folder or not os.path.isabs(folder):
        return False, None  # damaged: the legacy keys decide
    path = Path(folder)
    if path.parent.name != NADARE_DIR:
        return False, None  # the default library (disk-dependent) or damage
    return True, path


def _legacy_game_folder(
    profile: Mapping[str, Any],
    name: str,
    default_root: str | os.PathLike[str] | None = None,
) -> Path | None:
    """:func:`game_folder` from the legacy keys alone (what the record is built from)."""
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list) and len(dirs) == 1 and _text(dirs[0]) is not None:
        folder = Path(os.path.expanduser(dirs[0]))
        return folder if folder.parent.name == NADARE_DIR else None
    if dirs:
        return None  # several, or damaged: never guess which one is the game's
    legacy = _legacy_library_folder(profile)
    if legacy is not None:
        return legacy
    root = Path(default_root) if default_root is not None else paths.profiles_dir()
    recorded = _recorded_default_folder(profile, _real(root))
    if recorded is not None:
        return recorded
    own = root / name
    if own.is_symlink():
        return None  # a link put there (to Documents, say) is never Nadare's folder (B1)
    # The default library's own folder only when the game keeps everything in it: an
    # imported game's folders are elsewhere, and claiming profiles/<name> for it put Nadare's
    # manifest and "made by Nadare" on a folder the game does not use (#1372 review D1).
    return own if _keeps_its_game_in(profile, own) else None


@dataclass(frozen=True)
class Claim:
    """A folder another game uses: ``owner`` (profile name), its ``title`` and why."""

    owner: str
    title: str
    folder: Path
    #: ``path`` (a recorded install/prefix), ``owned_dir``, ``game_folder`` or ``profiles``.
    source: str
    #: The folder as written (no link followed); ``folder`` is where it really is. Both count:
    #: a link inside a game's folder, or a linked ``profiles/<name>``, must not hide either side
    #: (review of #1376).
    literal: Path | None = None


#: Profile keys whose folders a game uses (``wineprefix`` is the old name for ``prefix``).
_USED_KEYS = ("install_dir", "prefix", "wineprefix")


def _literal(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))


def _damaged(folder: Path, home: Path) -> bool:
    """Home, a folder holding it, or ``/``: never one game's own folder, so a record naming it
    is damage. A drive's mount point is not damage: a game can sit on its own drive."""
    return folder == home or folder in home.parents or folder == Path("/")


def _broad(folder: Path, home: Path) -> bool:
    """Home, a folder holding it, the root or a drive: nobody's game folder (#1207 b077)."""
    try:
        return folder == home or folder in home.parents or os.path.ismount(folder)
    except OSError:
        return True


def claims(profiles: Mapping[str, Any], *, excluding: str | None = None) -> list[Claim]:
    """Every folder each game uses (#1372 step 2): the one answer to "used by another game".

    Recorded paths, ``owned_dirs``, the game's own folder (:func:`game_folder`) and its
    ``profiles/<name>`` (it holds the game's state and default prefix even when the profile names
    no folder). ``excluding`` leaves one profile out (the one asking).
    """
    found: list[Claim] = []
    home = _real(Path.home())
    root = paths.profiles_dir()
    try:
        real_root = root.resolve()
    except OSError:
        real_root = root
    for name, profile in profiles.items():
        if name == excluding or not isinstance(profile, Mapping):
            continue
        owner = str(name)
        title = str(profile.get("title") or owner)
        for key in _USED_KEYS:
            value = _setting(profile, key)
            if value is not None:
                found.append(Claim(owner, title, _real(value), "path", _literal(value)))
        dirs = profile.get("owned_dirs")
        if isinstance(dirs, list):
            for entry in dirs:
                value = _text(entry)
                # Nadare never records home, /, or a whole drive as a game's folder: such an
                # entry is damage and must not make every folder "used" (review of #1376).
                if value is not None and not _damaged(_real(value), home):
                    found.append(Claim(owner, title, _real(value), "owned_dir", _literal(value)))
        folder = game_folder(profile, owner)
        if folder is not None:
            found.append(Claim(owner, title, _real(folder), "game_folder", _literal(folder)))
        own = real_root / owner
        real_own = _real(own)
        # Where a linked profiles/<name> really leads counts as that game's too, unless the
        # link is damage (home, a folder holding it, or /).
        if _damaged(real_own, home):
            real_own = own
        found.append(Claim(owner, title, real_own, "profiles", own))
    return found


def overlapping(
    profiles: Mapping[str, Any],
    path: str | os.PathLike[str],
    *,
    excluding: str | None,
    direction: str = "either",
    ignore_broad: bool = True,
) -> Claim | None:
    """The first other game's folder that ``path`` overlaps, or ``None``.

    ``direction``: ``inside`` (``path`` is that folder or inside it), ``contains`` (that folder
    is ``path`` or inside it) or ``either``. A broad recorded folder (home, a folder holding it,
    ``/``, a drive) only counts when it is ``path`` itself, unless ``ignore_broad`` is false:
    deletion checks count every recorded folder, so they only ever refuse more.
    """
    target = _real(path)
    home = _real(Path.home())
    target_literal = _literal(path)
    for claim in claims(profiles, excluding=excluding):
        # Every combination of the written and the real forms: a link on either side (a game
        # folder holding a link, a library under a linked /home) must not hide an overlap.
        mine = {claim.folder}
        if claim.literal is not None:
            mine.add(claim.literal)
            # Where the entry itself sits (its folders resolved, its own name kept): a link
            # recorded inside a game folder that lies under a linked library root.
            try:
                mine.add(_real(claim.literal.parent) / claim.literal.name)
            except (OSError, ValueError):
                pass
        pairs = [(folder, where) for folder in mine for where in {target, target_literal}]
        for folder, where in pairs:
            if folder == where:
                return claim
            if ignore_broad and claim.source == "path" and _broad(folder, home):
                continue
            inside = folder in where.parents
            contains = where in folder.parents
            if (direction in ("inside", "either") and inside) or (
                direction in ("contains", "either") and contains
            ):
                return claim
    return None


__all__ = [
    "FOLDER_KEYS",
    "NADARE_DIR",
    "Claim",
    "catalog_kind",
    "claims",
    "game_folder",
    "overlapping",
]


# ---- phase 2a: the stored record ---------------------------------------------------------
#: The profile key that holds the record (#1372 phase 2; DECISIONS 2026-11-15).
RECORD_KEY = "location"

#: Legacy keys the record is built from; a copy is kept under ``from`` so a reader can see
#: that an older Nadare (which edits only these) changed them since (legacy then wins).
_RECORD_SOURCES = ("install_dir", "prefix", "owned_trees", "owned_dirs")

#: Legacy owned-tree names and the record's short names (the manifest's).
_TREE_NAMES = {"install_dir": "install", "prefix": "prefix"}


def _record_sources(profile: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _RECORD_SOURCES:
        value = _setting(profile, key) if key in _TREE_NAMES else profile.get(key)
        out[key] = copy.deepcopy(value) if value is not None else None
    return out


def _relative_to(value: str | None, folder: Path | None) -> str | None:
    if value is None:
        return None
    path = _literal(value)
    if folder is not None:
        base = _literal(folder)
        if path == base:
            return "."
        if base in path.parents:
            return path.relative_to(base).as_posix()
    return str(path)


def build_record(profile: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The ``location`` record for ``profile``, from today's answers; ``None`` outside catalog.

    The manifest's shape: the game folder, install and prefix (relative when inside it),
    and which of them Nadare made; plus ``from``, the legacy values it was built from.
    """
    if not catalog_kind(profile.get("backend")):
        return None
    folder = _legacy_game_folder(profile, name)  # never from the record it replaces
    owned = profile.get("owned_trees")
    owned = owned if isinstance(owned, list) else []
    return {
        "folder": str(folder) if folder is not None else None,
        "install": _relative_to(_setting(profile, "install_dir"), folder),
        "prefix": _relative_to(_setting(profile, "prefix"), folder),
        "made_by_nadare": [_TREE_NAMES[k] for k in ("install_dir", "prefix") if k in owned],
        "from": _record_sources(profile),
    }


def record_is_current(profile: Mapping[str, Any]) -> bool:
    """Whether the stored record was built from the legacy values the profile has now."""
    record = profile.get(RECORD_KEY)
    if not isinstance(record, dict):
        return False
    return record.get("from") == _record_sources(profile)


def refresh_record(profile: dict[str, Any], name: str) -> bool:
    """Store a current record in ``profile`` (or drop a stale one); True when it changed."""
    try:
        record = build_record(profile, name)
    except (OSError, RuntimeError, ValueError):  # an unreadable folder: keep what is stored
        return False
    if record is None:
        return profile.pop(RECORD_KEY, None) is not None
    if profile.get(RECORD_KEY) == record:
        return False
    profile[RECORD_KEY] = record
    return True
