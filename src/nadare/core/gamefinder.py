# SPDX-License-Identifier: GPL-3.0-or-later
"""Find a catalog game's copies on disk and point its profile at one (#1293).

b21 put a picked game in ``X/Nadare/Nadare/<Game>`` and b22 puts it in ``X/Nadare/<Game>``.
A profile written by one rule can therefore point at folders the other rule never made. This
module reads both layouts (and a profile's own recorded ancestors), lists the working copies
best first, and lets the user re-point the profile without touching any game file.

Everything here only reads except :func:`use`, which changes the profile in memory (the caller
saves the config) and writes the chosen game folder's manifest.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gamefolders, libraries, relink
from .gameversion import (
    INCOMPLETE_MARKER,
    VERSION_FILE,
    read_czn_version,
    read_install_version,
)

#: Manifest folder field -> profile key.
_FIELDS = (("install", "install_dir"), ("prefix", "prefix"))


@dataclass(frozen=True)
class Copy:
    """One game folder that can be used for a profile."""

    game_dir: Path
    library: Path | None
    install_dir: Path | None
    prefix: Path
    made_by_nadare: tuple[str, ...]
    version: str | None
    complete: bool
    recorded: bool
    from_manifest: bool
    kind: str


# ---- paths and folders ------------------------------------------------------------------------


def _real(value: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.path.realpath(os.path.expanduser(str(value))))
    except ValueError:
        # A hand-edited path with a NUL byte cannot name a folder; /dev/null is never one.
        return Path(os.devnull)


def _path_text(value: object) -> str | None:
    """The value as a path string, or None when it cannot name anything (blank or a NUL)."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return value


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _folder_values(profile: Mapping[str, Any]) -> Iterable[Path]:
    for key in ("install_dir", "prefix", "wineprefix"):
        value = _path_text(profile.get(key))
        if value is not None:
            yield _real(value)


def _recorded_game(profile: Mapping[str, Any]) -> Path | None:
    """The profile's own game folder: its owned folder, else the parent of a recorded path."""
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list):
        for entry in dirs:
            value = _path_text(entry)
            if value is not None:
                return _real(value)
    for value in _folder_values(profile):
        return value.parent
    return None


def _profile_folders(profile: Mapping[str, Any], name: str) -> Iterable[Path]:
    """Every folder another profile records, including the game folder Add Game made."""
    dirs = profile.get("owned_dirs")
    if isinstance(dirs, list):
        for entry in dirs:
            value = _path_text(entry)
            if value is not None:
                yield _real(value)
    yield from _folder_values(profile)
    game_dir = libraries._game_folder(profile, name, None)
    if game_dir is not None:
        yield _real(game_dir)


def _used_by_other(profiles: Mapping[str, Any], name: str, game_dir: Path) -> bool:
    """True when another profile keeps the copy itself or something inside it."""
    for other_name, other in profiles.items():
        if other_name == name or not isinstance(other, Mapping):
            continue
        if any(_inside(path, game_dir) for path in _profile_folders(other, str(other_name))):
            return True
    return False


def _has_drive_c(prefix: Path) -> bool:
    return (prefix / "drive_c").is_dir() or (prefix / "pfx" / "drive_c").is_dir()


def _library_of(game_dir: Path) -> Path | None:
    return libraries.library_of_game_dir(game_dir)


def _bases(config, profile: Mapping[str, Any]) -> set[Path]:
    """Every folder whose direct children may be game folders, in either layout."""
    bases: set[Path] = set()
    default = libraries.default_root()
    for root in libraries.library_roots(config):
        if root == default:
            continue  # profiles/<name>, not games: another profile's folder is not a copy
        base = root / libraries.NADARE_DIR
        bases.add(_real(base))
        bases.add(_real(root / libraries.NADARE_DIR / libraries.NADARE_DIR))
        if root.name == libraries.NADARE_DIR:
            bases.add(_real(root))
    recorded = _recorded_game(profile)
    if recorded is not None:
        ancestor = recorded.parent
        while ancestor.name == libraries.NADARE_DIR:
            bases.add(_real(ancestor))
            bases.add(_real(ancestor / libraries.NADARE_DIR))
            ancestor = ancestor.parent
    return bases


def _subfolders(base: Path) -> Iterable[Path]:
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        yield _real(entry)


# ---- what a folder is -------------------------------------------------------------------------


def _read_version(
    install_dir: Path | None, prefix: Path | None = None, kind: str = ""
) -> str | None:
    version = read_install_version(install_dir)
    if version is None and kind == "stove-czn":
        version = read_czn_version(prefix)  # CZN records its version in the UPF
    return version


def _non_empty(folder: Path) -> bool:
    try:
        return next(folder.iterdir(), None) is not None
    except OSError:
        return False


def _complete(install_dir: Path | None, prefix: Path, spec: libraries.GameSpec | None) -> bool:
    try:
        if install_dir is not None:
            if not install_dir.is_dir():
                return False
            marker = install_dir / INCOMPLETE_MARKER
            if marker.exists() or marker.is_symlink():
                return False
            if spec is None:
                if not _non_empty(install_dir):
                    return False
            elif not libraries._find_file(install_dir, spec.exes, 2):
                return False
            return _has_drive_c(prefix)
        if not _has_drive_c(prefix):
            return False
        if spec is None:
            return True
        inner = prefix / spec.install_root / spec.install_subdir
        return inner.is_dir() and libraries._find_file(inner, spec.exes, 1)
    except OSError:
        return False  # a recorded path over NAME_MAX is damaged, not a reason to crash the scan


def _candidate(
    config,
    name: str,
    profile: Mapping[str, Any],
    game_dir: Path,
    spec: libraries.GameSpec | None,
) -> Copy | None:
    manifest_file = game_dir / libraries.MANIFEST_NAME
    if manifest_file.is_symlink():
        return None
    if manifest_file.exists():
        manifest = libraries.read_manifest(game_dir)
        if manifest is None or manifest.kind != profile.get("backend"):
            return None
        install_dir = manifest.install_dir
        prefix = manifest.prefix
        made = manifest.made_by_nadare
        from_manifest = True
    else:
        if spec is None:
            return None
        found = libraries.match_game(game_dir, spec)
        if found is None:
            return None
        install_dir, prefix = found
        made = ()
        from_manifest = False
        if prefix is None:
            return None
    if prefix is None or _used_by_other(config.profiles, name, game_dir):
        return None
    return Copy(
        game_dir=game_dir,
        library=_library_of(game_dir),
        install_dir=install_dir,
        prefix=prefix,
        made_by_nadare=made,
        version=_read_version(install_dir, prefix, str(profile.get("backend") or "")),
        complete=_complete(install_dir, prefix, spec),
        recorded=_recorded_game(profile) == game_dir,
        from_manifest=from_manifest,
        kind=str(profile.get("backend") or ""),
    )


# ---- ranking ----------------------------------------------------------------------------------


def _version_tuple(version: str) -> tuple[tuple[int, Any], ...]:
    """Parts that compare without ever putting an int next to a string."""
    parts: list[tuple[int, Any]] = []
    for token in version.replace("_", ".").replace("-", ".").replace("+", ".").split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token.lower()))
    return tuple(parts)


class _Descending:
    """Wrap a value so ``sorted`` puts larger values first."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other: _Descending) -> bool:
        return other.value < self.value


def _rank(copy: Copy) -> tuple:
    return (
        0 if copy.complete else 1,
        0 if copy.version is not None else 1,
        _Descending(_version_tuple(copy.version) if copy.version is not None else ()),
        str(copy.game_dir),
    )


def best(copies: Iterable[Copy]) -> Copy | None:
    """The best copy, whatever order the caller lists them in."""
    ordered = sorted(copies, key=_rank)
    return ordered[0] if ordered else None


# ---- public searches --------------------------------------------------------------------------


def folders_missing(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """The profile's recorded folder keys that are set but not there; empty outside the model."""
    if not gamefolders.in_model(profile):
        return ()
    missing = []
    for key in gamefolders.FOLDER_KEYS:
        value = _path_text(profile.get(key))
        if value is None:
            continue
        try:
            exists = Path(os.path.expanduser(value)).exists()
        except (OSError, ValueError):
            exists = False
        if not exists:
            missing.append(key)
    return tuple(missing)


def find_copies(config, name: str, spec: libraries.GameSpec | None) -> list[Copy]:
    """Every usable copy of ``name``'s game, best first; ``spec`` enables guessing."""
    profile = config.profiles.get(name)
    if not isinstance(profile, Mapping):
        return []
    found: list[Copy] = []
    seen: set[Path] = set()
    for base in sorted(_bases(config, profile)):
        for game_dir in _subfolders(base):
            if game_dir in seen:
                continue
            seen.add(game_dir)
            copy = _candidate(config, name, profile, game_dir, spec)
            if copy is not None:
                found.append(copy)
    return sorted(found, key=_rank)


def _game_files_missing(profile: Mapping[str, Any]) -> bool:
    """Whether the folder that holds the game itself is gone.

    That is ``install_dir`` when the profile records one; only a prefix-only game (NIKKE, CZN)
    keeps the game in its prefix. A deleted prefix next to existing game files is not a broken
    install: Nadare makes a new prefix at the next launch.
    """
    missing = folders_missing(profile)
    install = profile.get("install_dir")
    key = "install_dir" if isinstance(install, str) and install.strip() else "prefix"
    return key in missing


def _recorded_library_exists(profile: Mapping[str, Any]) -> bool:
    """Whether the library around the profile's recorded game folder is mounted."""
    recorded = _recorded_game(profile)
    if recorded is None:
        return False
    library = libraries.library_root_for(recorded)
    if library is not None:
        return library.exists()
    return recorded.parent.exists()


def repairs(config, specs: Iterable[libraries.GameSpec]) -> dict[str, list[Copy]]:
    """Profiles whose game files are gone but found elsewhere, with their copies best first.

    The profile's own recorded folder is never offered back: using it would change nothing.
    An unplugged drive is not a moved game: the library around the recorded folder must be
    there before another copy is offered.
    """
    known = list(specs)
    out: dict[str, list[Copy]] = {}
    for name, profile in config.profiles.items():
        if (
            not isinstance(profile, Mapping)
            or not _game_files_missing(profile)
            or not _recorded_library_exists(profile)
        ):
            continue
        spec = next((item for item in known if item.kind == profile.get("backend")), None)
        copies = [c for c in find_copies(config, name, spec) if not c.recorded]
        if copies:
            out[str(name)] = copies
    return out


# ---- using a copy -----------------------------------------------------------------------------


def _is_running(status: object | None) -> bool:
    return status is not None and (
        getattr(status, "state", "") == "running" or bool(getattr(status, "running", False))
    )


def running_problem(
    probe: Callable[[], object], known: object | None = None
) -> str | None:
    """Why a profile's folder must not change now, or None when the game is not running.

    The caller asks its backend right before the change (the page's answer may be old).
    ``known`` is a status already in hand; a running game wins immediately. An unanswerable
    probe refuses too: an unknown state could be running.
    """
    if _is_running(known):
        return "The game is running. Close it first."
    try:
        status = probe()
    except Exception as exc:  # noqa: BLE001 - unknown means do not re-point
        return f"Could not check whether the game is running ({exc})."
    if _is_running(status):
        return "The game is running. Close it first."
    return None


def use(config, name: str, copy: Copy) -> None:
    """Point the profile ``name`` at ``copy``. The caller saves the config.

    Nothing on disk is moved; the only file written is the chosen folder's manifest. Every
    check runs before the profile is changed, so a refusal leaves the config untouched.
    """
    profile = config.profiles.get(name)
    if not isinstance(profile, Mapping):
        raise relink.RelinkError(f"There is no game named {name}.")
    if not gamefolders.in_model(profile):
        raise relink.RelinkError("This game is not kept in a Nadare game folder.")
    if not _game_files_missing(profile):
        raise relink.RelinkError("The game's files are back in the folder the profile records.")
    if copy.kind != profile.get("backend"):
        raise relink.RelinkError("That folder belongs to a different game.")

    game_dir = Path(copy.game_dir)
    if game_dir.is_symlink() or not game_dir.is_dir():
        raise relink.RelinkError(f"The game folder {game_dir} is not there any more.")
    prefix = Path(copy.prefix)
    if prefix.exists() and not _has_drive_c(prefix):
        raise relink.RelinkError(f"{prefix} does not look like a Wine prefix (no drive_c).")
    install_dir = Path(copy.install_dir) if copy.install_dir is not None else None
    if install_dir is not None and not install_dir.is_dir():
        raise relink.RelinkError(f"The game folder {install_dir} is not there any more.")
    if _used_by_other(config.profiles, name, game_dir):
        raise relink.RelinkError("Another game already uses that folder.")

    if install_dir is None:
        profile.pop("install_dir", None)
    else:
        profile["install_dir"] = str(install_dir)
    profile["prefix"] = str(prefix)

    if copy.from_manifest:
        made = set(copy.made_by_nadare)
        trees = [key for field, key in _FIELDS if field in made]
        profile["owned_trees"] = trees
        profile["owned_paths"] = {key: str(profile[key]) for key in trees}
        if trees:
            profile["owned_dirs"] = [str(game_dir)]
        else:
            profile.pop("owned_dirs", None)
        if "prefix" in made:
            profile["installed_prefix"] = str(prefix)
        # The manifest already describes this folder: rewriting it would only change its
        # folder's mtime, and a repair must not look like a change to the game files.
        if not libraries._matches(libraries.read_manifest(game_dir), profile):
            libraries.write_manifest(profile, game_dir)
    else:
        profile.pop("owned_trees", None)
        profile.pop("owned_paths", None)
        profile.pop("owned_dirs", None)

    if copy.library is not None and Path(copy.library) != libraries.default_root():
        try:
            libraries.add_library(config, copy.library)
        except libraries.LibraryError:
            pass


def use_and_save(config, name: str, chosen: Copy) -> None:
    """Point ``name`` at ``chosen`` and save, restoring the config when anything is refused."""
    profile = config.profiles.get(name)
    backup = copy.deepcopy(profile) if isinstance(profile, Mapping) else None
    libraries_backup = copy.deepcopy(config.data.get(libraries.CONFIG_KEY))
    try:
        with config.locked():
            use(config, name, chosen)
            config.save()
    except (relink.RelinkError, OSError):
        with config.locked():
            if backup is not None:
                current = config.profiles.get(name)
                if isinstance(current, dict):
                    current.clear()
                    current.update(backup)
                else:
                    config.profiles[name] = backup
            if libraries_backup is None:
                config.data.pop(libraries.CONFIG_KEY, None)
            else:
                config.data[libraries.CONFIG_KEY] = libraries_backup
        raise


__all__ = [
    "Copy",
    "INCOMPLETE_MARKER",
    "VERSION_FILE",
    "best",
    "find_copies",
    "folders_missing",
    "repairs",
    "running_problem",
    "use",
    "use_and_save",
]
