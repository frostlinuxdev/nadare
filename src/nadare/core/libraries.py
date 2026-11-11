# SPDX-License-Identifier: GPL-3.0-or-later
"""Game libraries, the way Steam has them: roots, a manifest per game, scan and adopt.

A library is a folder the user chose for games. Nadare only ever writes inside
``<library>/Nadare/<Game>/`` (as Steam only writes inside ``steamapps/``), so a library never
has to be empty, and removing one only forgets it: no file is touched, and adding it back is
always allowed. The default library is :func:`paths.profiles_dir`, where a game's folder is
``profiles/<name>/``; it is always there and cannot be removed.

Each game folder Nadare makes gets a small ``nadare-game.json`` (kind, title, and the install
folder and prefix RELATIVE to the game folder), so a library added back, or found again after
a lost config, shows which games it holds. That file is untrusted input: anyone can put one
there. Only catalog games (one profile per game) are adopted from it, never the generic
Wine, Steam or script kinds, which carry commands; every path must stay inside the game
folder, symlinks included.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import backends, flatpakfs, paths, prefixguard
from .config import MAX_PROFILE_NAME, valid_profile_name
from .profilenames import short_name

NADARE_DIR = "Nadare"
MANIFEST_NAME = "nadare-game.json"
MANIFEST_SCHEMA = 1
MANIFEST_MAX_BYTES = 16 * 1024
CONFIG_KEY = "libraries"

#: Manifest field -> profile key, for the folders a manifest may name.
_FOLDERS = {"install": "install_dir", "prefix": "prefix"}
_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class LibraryError(ValueError):
    """A library cannot be added or removed; the message is shown to the user."""


@dataclass(frozen=True)
class Manifest:
    game_dir: Path
    kind: str
    title: str
    install_dir: Path | None
    prefix: Path | None
    made_by_nadare: tuple[str, ...]


#: What :func:`scan` finds: a game on disk that no profile uses yet.
Candidate = Manifest


# ---- roots ------------------------------------------------------------------------------------


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def default_root() -> Path:
    return _real(paths.profiles_dir())


def _stored(config) -> list[Any]:
    value = config.data.get(CONFIG_KEY)
    return value if isinstance(value, list) else []


def library_roots(config) -> list[Path]:
    """The default library first, then the ones the user added (damaged entries ignored)."""
    roots = [default_root()]
    for entry in _stored(config):
        if not isinstance(entry, Mapping):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not os.path.isabs(os.path.expanduser(raw)):
            continue
        root = _real(raw)
        if root not in roots:
            roots.append(root)
    return roots


def _unsafe_reason(root: Path) -> str:
    home = _real(Path.home())
    if root == Path("/"):
        return "the top of the filesystem"
    if root == home:
        return "your home folder"
    if root in home.parents:
        return "a folder that contains your home folder"
    if root.parent == home and root.name in prefixguard.PERSONAL_FOLDERS:
        return "one of your personal folders"
    return ""


def display_path(root: str | os.PathLike[str]) -> str:
    """``root`` for showing: ``~/Games`` under home, the full path elsewhere."""
    path = Path(root)
    home = _real(Path.home())
    try:
        rest = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not rest.parts else f"~/{rest.as_posix()}"


def library_of(folder: str | os.PathLike[str]) -> Path:
    """The library a folder picked for a game means.

    Usually the folder itself (games go in ``<folder>/Nadare/<Game>``). The ``Nadare`` folder
    of a library means that library: picking where the games already are must not give
    ``<library>/Nadare/Nadare/<Game>`` (maintainer, 2026-11-11).
    """
    path = Path(os.path.normpath(os.path.expanduser(str(folder))))
    # Only when the parent may be a library: ``~/Nadare`` stays a library of its own (home
    # cannot be one), as it was.
    if path.name == NADARE_DIR and path.parent != path and not _unsafe_reason(_real(path.parent)):
        return path.parent
    return path


def add_library(config, path: str | os.PathLike[str]) -> Path:
    """Remember ``path`` as a library and return it resolved. The caller saves the config.

    A library may hold other files: games go in ``<path>/Nadare/<Game>``. The top of a drive is
    fine for the same reason. Refused: the filesystem root, home, a folder containing home, a
    personal folder, and a library that is already listed.
    """
    text = os.path.expanduser(str(path))
    if not str(path).strip() or not os.path.isabs(text):
        raise LibraryError(f"{path} is not a full folder path.")
    root = _real(text)
    reason = _unsafe_reason(root)
    if reason:
        raise LibraryError(
            f"{root} is {reason}. Pick a folder inside it, for example {root / 'Games'}."
        )
    out_of_reach = flatpakfs.unreachable_reason(root)
    if out_of_reach:
        raise LibraryError(out_of_reach)
    if root.exists() and not root.is_dir():
        raise LibraryError(f"{root} is a file, not a folder.")
    if root in library_roots(config):
        raise LibraryError(f"{root} is already a library.")
    config.data[CONFIG_KEY] = [*_stored(config), {"path": str(root)}]
    return root


def remove_library(config, path: str | os.PathLike[str]) -> bool:
    """Forget the library ``path``; no file is touched. ``False`` when it was not listed."""
    root = _real(path)
    if root == default_root():
        raise LibraryError("The default library cannot be removed.")
    kept, removed = [], False
    for entry in _stored(config):
        raw = entry.get("path") if isinstance(entry, Mapping) else None
        if isinstance(raw, str) and _real(raw) == root:
            removed = True
            continue
        kept.append(entry)
    if removed:
        config.data[CONFIG_KEY] = kept
    return removed


# ---- manifest ---------------------------------------------------------------------------------


def _inside(path: Path, game_dir: Path) -> bool:
    try:
        path.relative_to(game_dir)
    except ValueError:
        return False
    return True


def write_manifest(profile: Mapping[str, Any], game_dir: str | os.PathLike[str]) -> Path | None:
    """Write ``game_dir/nadare-game.json`` for ``profile``; ``None`` when it does not fit.

    Every folder the profile names must lie inside ``game_dir``; a game whose folders are
    elsewhere is not a library game and gets no manifest.
    """
    folder = _real(game_dir)
    data: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "backend": str(profile.get("backend") or ""),
        "title": str(profile.get("title") or ""),
    }
    owned = profile.get("owned_trees")
    owned = owned if isinstance(owned, list) else []
    made: list[str] = []
    for field, key in _FOLDERS.items():
        value = profile.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        target = _real(value)
        if not _inside(target, folder):
            return None
        data[field] = target.relative_to(folder).as_posix() or "."
        if key in owned:
            made.append(field)
    if not any(field in data for field in _FOLDERS):
        return None
    data["made_by_nadare"] = made
    target = folder / MANIFEST_NAME
    tmp = folder / f".{MANIFEST_NAME}.tmp"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        # The game folder is writable by the game: never follow a link planted at the temp
        # name (it would overwrite the link's target), and never reuse what is there.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, target)
    except OSError:
        try:
            if not tmp.is_symlink():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _game_folder(
    profile: Mapping[str, Any], name: str, default_root: str | os.PathLike[str] | None
) -> Path | None:
    """The folder Nadare made for this catalog game, or ``None`` (imported, generic kind).

    It is the profile's ``<library>/Nadare/<Game>`` (its one ``owned_dirs`` entry), or
    ``profiles/<name>`` in the default library.
    """
    if not _catalog_kind(profile.get("backend")):
        return None
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list) and len(dirs) == 1 and isinstance(dirs[0], str):
        game_dir = Path(os.path.expanduser(dirs[0]))
        return game_dir if game_dir.parent.name == NADARE_DIR else None
    if dirs:
        return None
    return Path(default_root if default_root is not None else paths.profiles_dir()) / name


def record_new_game(
    profile: Mapping[str, Any], name: str, *, default_root: str | os.PathLike[str] | None = None
) -> Path | None:
    """Write the manifest for a game Nadare just made a folder for; ``None`` when it has none.

    An imported game, a generic kind and a folder Nadare cannot write get nothing; never an
    error, the game works without it.
    """
    game_dir = _game_folder(profile, name, default_root)
    if game_dir is None:
        return None
    try:
        return write_manifest(profile, game_dir)
    except OSError:
        return None


def _matches(manifest: Manifest | None, profile: Mapping[str, Any]) -> bool:
    if manifest is None:
        return False
    for folder, key in ((manifest.install_dir, "install_dir"), (manifest.prefix, "prefix")):
        value = profile.get(key)
        want = _real(value) if isinstance(value, str) and value.strip() else None
        if folder != want:
            return False
    return manifest.kind == profile.get("backend")


def sync_game(config, name: str, *, remember_library: bool = True) -> bool:
    """Bring an existing game's manifest and library up to date; ``True`` if the config changed.

    For a game Nadare laid out whose folder exists: (re)write its manifest when it is missing,
    damaged or stale (after a Move), and remember the library it is in (unless
    ``remember_library`` is false). Missing folders are never created. The caller saves the
    config when this returns ``True``.
    """
    profile = config.profiles.get(name)
    if not isinstance(profile, Mapping):
        return False
    game_dir = _game_folder(profile, name, None)
    if game_dir is None or game_dir.is_symlink() or not game_dir.is_dir():
        return False
    if not _matches(read_manifest(game_dir), profile):
        try:
            write_manifest(profile, game_dir)
        except OSError:
            pass
    if game_dir.parent.name != NADARE_DIR or not remember_library:
        return False
    try:
        add_library(config, game_dir.parent.parent)
    except LibraryError:
        return False
    return True


def _set_up(config) -> bool:
    """Whether the ``libraries`` setting holds the user's choice: a list that is empty (every
    library removed) or has at least one usable entry. A damaged value is not (round 6)."""
    value = config.data.get(CONFIG_KEY)
    if not isinstance(value, list):
        return False
    return not value or any(
        isinstance(entry, Mapping) and isinstance(entry.get("path"), str) for entry in value
    )


def folder_moved(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether a Move changed the game's own folder (``owned_dirs``), not only a prefix."""
    return before.get("owned_dirs") != after.get("owned_dirs")


def backfill(config) -> bool:
    """:func:`sync_game` for every game (games from before libraries existed); ``True`` if the
    config changed and should be saved.

    Libraries are remembered only while the setting does not exist yet (the first start with
    libraries; adding, picking or removing a library writes it). After that a library the user
    removed in Settings stays removed (round 4: it came back on every start); manifests are
    still written.
    """
    first = not _set_up(config)
    changed = False
    for name in list(config.profiles):
        changed = sync_game(config, name, remember_library=first) or changed
    return changed


def _catalog_kind(kind: object) -> bool:
    return isinstance(kind, str) and backends.one_per_game(kind)


def _read(game_dir: Path) -> Manifest:
    """The manifest in ``game_dir``; raises ``ValueError`` with a short reason."""
    file = game_dir / MANIFEST_NAME
    if file.is_symlink():
        raise ValueError("the manifest is a link")
    try:
        with open(file, "rb") as fh:
            raw = fh.read(MANIFEST_MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read it ({exc.strerror or exc})") from None
    if len(raw) > MANIFEST_MAX_BYTES:
        raise ValueError("the manifest is too big")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("the manifest is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("the manifest is not a JSON object")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unknown manifest version {data.get('schema')!r}")
    kind, title = data.get("backend"), data.get("title")
    if not _catalog_kind(kind):
        raise ValueError(f"{kind!r} is not a catalog game this Nadare knows")
    if not isinstance(title, str):
        raise ValueError("the title is not text")
    made = data.get("made_by_nadare", [])
    if not isinstance(made, list) or not all(isinstance(m, str) and m in _FOLDERS for m in made):
        raise ValueError("made_by_nadare is damaged")
    found: dict[str, Path | None] = {}
    for field in _FOLDERS:
        value = data.get(field)
        if value is None:
            found[field] = None
            continue
        if not isinstance(value, str) or not value or os.path.isabs(value):
            raise ValueError(f"{field} must be a path inside the game folder")
        target = _real(game_dir / value)
        if not _inside(target, game_dir):
            raise ValueError(f"{field} points outside the game folder")
        found[field] = target
    if found["install"] is None and found["prefix"] is None:
        raise ValueError("the manifest names no folder")
    return Manifest(
        game_dir=game_dir,
        kind=kind,
        title=title.strip() or kind,
        install_dir=found["install"],
        prefix=found["prefix"],
        made_by_nadare=tuple(m for m in made if found.get(m) is not None),
    )


def read_manifest(game_dir: str | os.PathLike[str]) -> Manifest | None:
    """The manifest in ``game_dir``, or ``None`` when there is none or it is unusable."""
    try:
        return _read(_real(game_dir))
    except ValueError:
        return None


# ---- scan and adopt ---------------------------------------------------------------------------


def _game_dirs(root: Path) -> list[Path]:
    base = root if root == default_root() else root / NADARE_DIR
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue  # a game folder Nadare made is a real folder
        manifest = entry / MANIFEST_NAME
        if manifest.exists() or manifest.is_symlink():
            found.append(entry)
    return found


def _used_folders(profiles: Mapping[str, Any]) -> set[Path]:
    used: set[Path] = set()
    for prof in profiles.values():
        if not isinstance(prof, Mapping):
            continue
        for key in ("install_dir", "prefix", "wineprefix"):
            value = prof.get(key)
            if isinstance(value, str) and value.strip():
                used.add(_real(value))
    return used


def scan(config, root: str | os.PathLike[str]) -> tuple[list[Candidate], list[str]]:
    """Games in the library ``root`` that no profile uses yet, and notes on what was skipped.

    Reads ``<root>/Nadare/*/nadare-game.json`` (for the default library ``profiles/*/``), one
    level only. A damaged or untrusted manifest is skipped with a note, never an error.
    """
    real_root = _real(root)
    used = _used_folders(config.profiles)
    found: list[Candidate] = []
    notes: list[str] = []
    for game_dir in _game_dirs(real_root):
        game_dir = _real(game_dir)
        try:
            manifest = _read(game_dir)
        except ValueError as exc:
            notes.append(f"{game_dir}: skipped, {exc}")
            continue
        folders = [p for p in (manifest.install_dir, manifest.prefix) if p is not None]
        if any(p in used for p in folders):
            continue  # already in the Library
        if not any(p.is_dir() for p in folders):
            notes.append(f"{game_dir}: skipped, only the manifest is left")
            continue
        other = backends.existing_profile(manifest.kind, config.profiles)
        if other is not None:
            notes.append(
                f"{game_dir}: skipped, {manifest.title} is already in your Library ({other})"
            )
            continue
        found.append(manifest)
    return found, notes


# ---- games whose manifest is gone (step 4) ------------------------------------------------------


@dataclass(frozen=True)
class GameSpec:
    """What a catalog game looks like on disk (from the Add Game catalog)."""

    kind: str
    title: str
    exes: tuple[str, ...] = ()
    install_root: str = ""
    install_subdir: str = ""


def exe_at_path(folder: Path, name: str) -> Path | None:
    """``folder/name`` when ``name`` is a relative path (NTE's ``Client/.../HTGame.exe``) to a
    regular file that stays inside ``folder``; ``None`` for a bare name or anything else."""
    if "/" not in name and "\\" not in name:
        return None
    relative = name.replace("\\", "/")
    if relative.startswith("/") or ".." in relative.split("/"):
        return None
    target = folder / relative
    try:
        if target.is_symlink() or not target.is_file():
            return None
        target.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return None
    return target


def _find_file(folder: Path, names: Iterable[str], depth: int) -> bool:
    """Whether one of ``names`` is a regular file in ``folder`` or up to ``depth`` levels below.

    A name that is a path is checked at exactly that place under ``folder``."""
    names = [n for n in names if n]
    if any(exe_at_path(folder, n) is not None for n in names):
        return True
    wanted = {n.lower() for n in names}
    stack = [(folder, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.name.lower() in wanted and entry.is_file():
                return True
            if level < depth and entry.is_dir():
                stack.append((entry, level + 1))
    return False


def match_game(game_dir: Path, spec: GameSpec) -> tuple[Path | None, Path | None] | None:
    """``(install_dir, prefix)`` when ``game_dir`` holds ``spec``'s program, else ``None``.

    Looks where Add Game lays a game out: ``install/`` (two levels deep) and, for a game the
    publisher's launcher installs, ``prefix/<install_root>/<install_subdir>/``.
    """
    if not spec.exes:
        return None
    install = game_dir / "install"
    prefix = game_dir / "prefix"
    has_prefix = (prefix / "drive_c").is_dir() and not prefix.is_symlink()
    if install.is_dir() and not install.is_symlink() and _find_file(install, spec.exes, 2):
        return install, prefix if has_prefix else None
    if has_prefix and spec.install_root:
        inner = prefix / spec.install_root / spec.install_subdir
        if inner.is_dir() and _find_file(inner, spec.exes, 1):
            return (install if install.is_dir() else None), prefix
    return None


def _guess(game_dir: Path, spec: GameSpec) -> Candidate | None:
    found = match_game(game_dir, spec)
    if found is None:
        return None
    install, prefix = found
    return Manifest(
        game_dir=game_dir,
        kind=spec.kind,
        title=spec.title or spec.kind,
        install_dir=_real(install) if install is not None else None,
        prefix=_real(prefix) if prefix is not None else None,
        made_by_nadare=(),  # a guess never claims Nadare made the files
    )


def find_unmarked(
    config, root: str | os.PathLike[str], specs: Iterable[GameSpec]
) -> list[Candidate]:
    """Game folders in ``<root>/Nadare`` without a manifest that hold a catalog game's program.

    Suggestions only: the caller asks before adopting, and an adopted guess owns nothing, so
    removing it later never deletes those files.
    """
    real_root = _real(root)
    if real_root == default_root():
        return []  # profiles/<name> holds every game's data; nothing to guess there
    usable = [spec for spec in specs if _catalog_kind(spec.kind)]
    try:
        entries = sorted((real_root / NADARE_DIR).iterdir())
    except OSError:
        return []
    used = _used_folders(config.profiles)
    found: list[Candidate] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        manifest = entry / MANIFEST_NAME
        if manifest.exists() or manifest.is_symlink():
            continue  # scan() reads it
        game_dir = _real(entry)
        if any(p == game_dir or game_dir in p.parents for p in used):
            continue
        for spec in usable:
            if backends.existing_profile(spec.kind, config.profiles) is not None:
                continue
            if any(c.kind == spec.kind for c in found):
                continue
            guess = _guess(game_dir, spec)
            if guess is not None:
                found.append(guess)
                break
    return found


def reusable_folder(config, game_dir: str | os.PathLike[str], spec: GameSpec) -> Candidate | None:
    """``game_dir`` described for reuse by a new install of ``spec``, or ``None``.

    Reused when no profile uses it and it holds this game: a manifest for this kind (whose
    ``made_by_nadare`` is kept), or else the game's program (then nothing is owned).
    """
    if not _catalog_kind(spec.kind):
        return None
    folder = Path(os.path.expanduser(str(game_dir)))
    if folder.is_symlink() or not folder.is_dir():
        return None
    folder = _real(folder)
    used = _used_folders(config.profiles)
    if any(p == folder or folder in p.parents for p in used):
        return None
    manifest = read_manifest(folder)
    if manifest is not None:
        if manifest.kind != spec.kind:
            return None
        named = [p for p in (manifest.install_dir, manifest.prefix) if p is not None]
        if not any(p.is_dir() for p in named):
            return None  # only the manifest is left: a fresh install, not "files already there"
        return manifest
    if (folder / MANIFEST_NAME).exists():
        return None  # damaged or another game's: do not guess over it
    return _guess(folder, spec)


def _slugify(text: str) -> str:
    slug = "".join(c if c in _SLUG_CHARS else "-" for c in text.strip().lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:MAX_PROFILE_NAME].strip("-")


def _free_name(base: str, taken: Iterable[str]) -> str:
    taken = set(taken)
    if base not in taken:
        return base
    index = 2
    while True:
        suffix = f"-{index}"
        candidate = f"{base[: MAX_PROFILE_NAME - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        index += 1


def adopt(config, candidate: Candidate) -> str:
    """Add a profile for ``candidate`` and return its name. The caller saves the config."""
    base = _slugify(short_name(candidate.kind) or candidate.title) or "game"
    name = _free_name(base, config.profiles)
    folder_name = candidate.game_dir.name
    if (
        candidate.game_dir.parent == default_root()
        and folder_name not in config.profiles
        and valid_profile_name(folder_name)
    ):
        # In the default library the game's folder is ``profiles/<profile name>``, and only that
        # folder of Nadare's own is deletable: keep the name it was made under (round 4).
        name = folder_name
    if not valid_profile_name(name):  # pragma: no cover - the slug rules make this unreachable
        raise LibraryError(f"cannot make a profile name for {candidate.title}")
    values: dict[str, Any] = {"backend": candidate.kind, "title": candidate.title}
    owned: list[str] = []
    for field, key in _FOLDERS.items():
        folder = getattr(candidate, "install_dir" if field == "install" else "prefix")
        if folder is None:
            continue
        values[key] = str(folder)
        if field in candidate.made_by_nadare:
            owned.append(key)
    if candidate.game_dir != default_root() / name:
        # In the default library profiles/<name> is the game's folder by its name; an
        # owned_dirs entry there would be one the manifest repair does not accept (round 6).
        values["owned_dirs"] = [str(candidate.game_dir)]
    if owned:
        values["owned_trees"] = owned
        values["owned_paths"] = {key: str(Path(values[key]).resolve()) for key in owned}
    config.add_profile(name, **values)
    return name


__all__ = [
    "CONFIG_KEY",
    "Candidate",
    "LibraryError",
    "MANIFEST_MAX_BYTES",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "GameSpec",
    "Manifest",
    "NADARE_DIR",
    "adopt",
    "backfill",
    "folder_moved",
    "add_library",
    "default_root",
    "display_path",
    "exe_at_path",
    "find_unmarked",
    "library_roots",
    "match_game",
    "read_manifest",
    "record_new_game",
    "remove_library",
    "reusable_folder",
    "scan",
    "sync_game",
    "write_manifest",
]
