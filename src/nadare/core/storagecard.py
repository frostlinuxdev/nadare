# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What the Settings "Disk space" card lists, sizes and clears (#1417, #1617).

Five kinds of data Nadare keeps that the player may want back:

* installer files in the known cache folders (WebView2, VC++, launcher installers);
* each Wine game's prefix ``Temp`` (``drive_c/users/*/AppData/Local/Temp``), except
  while that game runs;
* old Steam prefix copies next to ``compatdata`` (``<appid>.bak-pre-*``), one row
  each, with the newest of an app id marked so the card can keep it by default;
* the shader caches #1364 owns (shared Mesa, per-game VKD3D and DXVK);
* each launcher's embedded browser cache in a game's prefix (NIKKE's CEF, Snowbreak's
  CEF, WebView2 apps), only on request and never the profile's sign-in data.

Measuring and deleting are separate.  A missing folder is not listed.  Deleting a
row touches only the exact paths listed, never a link's target, and folders go
through :func:`nadare.core.removal.delete_tree` so a mounted drive inside survives.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    diskspace,
    installercache,
    paths,
    process,
    removal,
    runlock,
    shadercache,
    steamfix,
)

log = logging.getLogger(__name__)

#: ``<appid>.bak-pre-<fix>``, with the ``-2``/``-3`` suffix ``plan_backup`` adds.
_BACKUP = re.compile(r"^(?P<app>\d+)\.bak-pre-[^/]+$")
#: A size the card walks a cache folder for (larger than the launch log's tiny budget).
CACHE_SECONDS = 1.0
CACHE_ENTRIES = 200_000

#: The browser cache folders a Clear may empty, and the two below ``Service Worker``.
WEB_CACHE_NAMES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
)
WEB_CACHE_NESTED = (
    ("Service Worker", "CacheStorage"),
    ("Service Worker", "ScriptCache"),
)
#: A folder holding one of these keeps the player signed in, so it identifies a profile.
WEB_PROFILE_MARKERS = (
    "Cookies",
    "Network Persistent State",
    "Local State",
    "Preferences",
)
#: How far below ``AppData/Local|Roaming`` a profile may sit, and how much of a prefix is walked.
WEB_PROFILE_DEPTH = 5
WEB_PROFILE_DIRS = 2000

#: One game's VKD3D and DXVK caches together over this get a warning on its rows.
GAME_WARN_BYTES = 1024**3
#: Every shader cache together over this, or over ``TOTAL_WARN_SHARE`` of the free
#: space on Nadare's cache disk (whichever is smaller), get one warning line.
TOTAL_WARN_BYTES = 8 * 1024**3
TOTAL_WARN_SHARE = 0.05

#: The short line a large game's rows carry.
GAME_WARNING = (
    f"Large: over {diskspace.human_size(GAME_WARN_BYTES)}. Clearing is safe; "
    "the game may stutter while it rebuilds."
)


@dataclass(frozen=True)
class Installers:
    """Installer files in Nadare's cache that can be downloaded again."""

    files: tuple[Path, ...]
    size: int


@dataclass(frozen=True)
class PrefixTemp:
    """One game's prefix ``Temp`` folders and whether that game is running."""

    profile: str
    title: str
    prefix: Path
    folders: tuple[Path, ...]
    size: int
    running: bool


@dataclass(frozen=True)
class Backup:
    """One ``<appid>.bak-pre-*`` copy of a Steam prefix."""

    app_id: str
    path: Path
    compatdata: Path
    size: int
    newest: bool


@dataclass(frozen=True)
class ShaderCache:
    """One shader cache layer #1364 owns (Mesa, or a game's VKD3D or DXVK)."""

    key: str
    label: str
    profile: str | None
    folders: tuple[Path, ...]
    size: int | None
    running: bool
    prefix: Path | None = None
    prefixes: tuple[Path, ...] = ()
    #: A short line when this game's caches are large; empty otherwise.
    warning: str = ""
    #: The measured size of each folder in ``folders`` (used to count shared ones once).
    folder_sizes: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class WebCache:
    """One game's embedded browser profiles and the caches Nadare may clear.

    ``profiles`` are the detected user-data folders (``.../tbs_cache``,
    ``.../CEF/User Data``, ``.../EBWebView/Default``); only the cache folders inside
    them are sized and emptied, never the files that keep the player signed in.
    """

    profile: str
    title: str
    prefix: Path
    profiles: tuple[Path, ...]
    size: int
    running: bool


@dataclass(frozen=True)
class Report:
    """Everything the card measured; each tuple is empty when nothing was found."""

    installers: Installers = field(default_factory=lambda: Installers((), 0))
    temps: tuple[PrefixTemp, ...] = ()
    backups: tuple[Backup, ...] = ()
    shaders: tuple[ShaderCache, ...] = ()
    webcaches: tuple[WebCache, ...] = ()
    #: One line under the "Shader caches" heading when their total is large.
    shader_warning: str = ""


@dataclass
class ClearResult:
    """What a Clear button deleted, and what it left alone (with the reason)."""

    removed: list[Path] = field(default_factory=list)
    freed: int = 0
    kept: list[Path] = field(default_factory=list)
    reason: str = ""


# ---- measuring -------------------------------------------------------------------------------


def steamapps_dirs() -> list[Path]:
    """Every Steam library's ``steamapps`` folder (the scan's one discovery seam)."""
    return steamfix.steamapps_dirs()


def is_running(prefix: str | os.PathLike[str]) -> bool:
    """Whether Wine or a Nadare run lock is still busy in ``prefix``.

    An answer that cannot be found counts as running: the card then greys the row
    instead of deleting files under a game it could not inspect.
    """
    try:
        return bool(process.prefix_pids(prefix)) or runlock.running(prefix)
    except Exception:  # noqa: BLE001 - an uninspectable prefix must not look idle
        log.debug("could not check whether a game is running in %s", prefix, exc_info=True)
        return True


def _setting(profile: Mapping[str, Any], key: str) -> str | None:
    value = profile.get(key)
    if value in (None, ""):
        extra = profile.get("extra")
        if isinstance(extra, Mapping):
            value = extra.get(key)
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        return value
    return None


def _prefix(
    profile: Mapping[str, Any], name: str, *, for_running: bool = False
) -> Path | None:
    """The game's prefix; resolve links only for checking whether it is running."""
    raw = _setting(profile, "prefix") or _setting(profile, "wineprefix")
    if raw is None:
        if not for_running:
            return removal.default_prefix(name)
        raw = str(paths.profiles_dir() / name / "prefix")
    try:
        prefix = Path(raw).expanduser()
        if not prefix.is_absolute():
            return None
        if for_running:
            prefix = prefix.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if prefix.is_symlink() or not prefix.is_dir():
        return None
    return prefix


def _users(prefix: Path) -> Iterable[Path]:
    """The real user folders in ``drive_c/users`` (links and ``Public`` skipped)."""
    users = prefix / "drive_c" / "users"
    try:
        entries = sorted(users.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.name == "Public" or entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        yield entry


def _real_dir(folder: Path) -> bool:
    try:
        st = folder.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _cache_bytes(folder: Path) -> int | None:
    """Bounded cache size, or ``None`` when absent, unreadable or over budget."""
    if not _real_dir(folder):
        return None
    return shadercache.cache_size(folder, max_seconds=CACHE_SECONDS, max_entries=CACHE_ENTRIES)


def _installers() -> Installers:
    found = installercache.installers()
    return Installers(tuple(path for path, _size in found), sum(size for _path, size in found))


def _temps(profiles: Mapping[str, Any], probe: Callable[[Path], bool]) -> list[PrefixTemp]:
    rows: list[PrefixTemp] = []
    for name in sorted(profiles):
        profile = profiles.get(name)
        if not isinstance(profile, Mapping):
            continue
        prefix = _prefix(profile, name)
        if prefix is None:
            continue
        folders = []
        for user in _users(prefix):
            folder = user / "AppData" / "Local" / "Temp"
            if _is_temp(folder, prefix):
                folders.append(folder)
        if not folders:
            continue
        try:
            running = bool(probe(prefix))
        except Exception:  # noqa: BLE001 - an uninspectable game is shown as running
            running = True
        rows.append(
            PrefixTemp(
                profile=name,
                title=str(profile.get("title") or name),
                prefix=prefix,
                folders=tuple(folders),
                size=sum(diskspace.tree_bytes(folder) for folder in folders),
                running=running,
            )
        )
    return rows


def _web_cache_paths(profile: Path) -> tuple[Path, ...]:
    """Every cache folder a Clear may empty inside one browser profile."""
    flat = tuple(profile / name for name in WEB_CACHE_NAMES)
    nested = tuple(profile / parent / name for parent, name in WEB_CACHE_NESTED)
    return flat + nested


def _real_entry(path: Path) -> bool:
    """Browser identity markers must be regular files, never folders or links."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _looks_like_web_profile(folder: Path) -> bool:
    """A real folder with a sign-in marker and at least one cache folder Nadare owns."""
    if not _real_dir(folder):
        return False
    if not any(_real_entry(folder / name) for name in WEB_PROFILE_MARKERS):
        return False
    return any(
        _real_dir(path) and _no_link_on_path(path, folder)
        for path in _web_cache_paths(folder)
    )


def _subdirs(folder: Path, limit: int) -> list[Path]:
    """At most ``limit`` real, non-link subfolders, sorted by name."""
    found: list[Path] = []
    if limit <= 0:
        return found
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found.append(Path(entry.path))
                if len(found) >= limit:
                    break
    except OSError:
        pass
    return sorted(found)


def _web_profiles(prefix: Path) -> tuple[Path, ...]:
    """The embedded browser profiles under a prefix, bounded in depth and directories."""
    if not _no_link_on_path(prefix / "drive_c" / "users", prefix):
        return ()
    roots: list[Path] = []
    for user in _users(prefix):
        for area in ("Local", "Roaming"):
            root = user / "AppData" / area
            if _no_link_on_path(root, prefix) and _real_dir(root):
                roots.append(root)
            if len(roots) >= WEB_PROFILE_DIRS:
                break
        if len(roots) >= WEB_PROFILE_DIRS:
            break
    roots.sort()
    pending: deque[tuple[Path, int]] = deque((root, 0) for root in roots)
    found: list[Path] = []
    budget = WEB_PROFILE_DIRS
    while pending and budget > 0:
        folder, depth = pending.popleft()
        budget -= 1
        if depth and _looks_like_web_profile(folder):
            found.append(folder)
        if depth >= WEB_PROFILE_DEPTH:
            continue
        for child in _subdirs(folder, budget - len(pending)):
            pending.append((child, depth + 1))
    return tuple(sorted(found))


def _webcaches(profiles: Mapping[str, Any], probe: Callable[[Path], bool]) -> list[WebCache]:
    rows: list[WebCache] = []
    for name in sorted(profiles):
        profile = profiles.get(name)
        if not isinstance(profile, Mapping):
            continue
        prefix = _prefix(profile, name)
        if prefix is None:
            continue
        found = _web_profiles(prefix)
        if not found:
            continue
        try:
            running = bool(probe(prefix))
        except Exception:  # noqa: BLE001 - an uninspectable game is shown as running
            running = True
        rows.append(
            WebCache(
                profile=name,
                title=str(profile.get("title") or name),
                prefix=prefix,
                profiles=found,
                size=sum(
                    _cache_bytes(path) or 0
                    for browser in found
                    for path in _web_cache_paths(browser)
                    if _no_link_on_path(path, browser)
                ),
                running=running,
            )
        )
    return rows


def _backups(steamapps: Iterable[str | os.PathLike[str]] | None) -> list[Backup]:
    roots = steamapps_dirs() if steamapps is None else steamapps
    found: dict[str, list[tuple[Path, os.stat_result]]] = {}
    seen: set[Path] = set()
    for root in roots:
        compatdata = Path(root) / "compatdata"
        try:
            key = compatdata.resolve()
        except OSError:
            key = compatdata
        if key in seen or compatdata.is_symlink() or not _real_dir(compatdata):
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(compatdata))
        except OSError:
            continue
        for entry in entries:
            match = _BACKUP.match(entry.name)
            if not match:
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            found.setdefault(match.group("app"), []).append((Path(entry.path), st))
    rows: list[Backup] = []
    for app_id in sorted(found):
        # copytree preserves the source mtime; ctime records the completed copy.
        members = sorted(found[app_id], key=lambda item: (item[1].st_ctime_ns, item[0].name))
        for index, (path, _st) in enumerate(members):
            rows.append(
                Backup(
                    app_id=app_id,
                    path=path,
                    compatdata=path.parent,
                    size=diskspace.tree_bytes(path),
                    newest=index == len(members) - 1,
                )
            )
    return rows


def _warn_games(rows: list[ShaderCache]) -> list[ShaderCache]:
    """Put one line on the first measured row of a game whose caches pass the limit."""
    totals: dict[str, int] = {}
    for row in rows:
        if row.profile is not None and row.size is not None:
            totals[row.profile] = totals.get(row.profile, 0) + row.size
    warned: set[str] = set()
    marked: list[ShaderCache] = []
    for row in rows:
        profile = row.profile
        if (
            profile is not None
            and row.size is not None
            and profile not in warned
            and totals.get(profile, 0) > GAME_WARN_BYTES
        ):
            warned.add(profile)
            marked.append(replace(row, warning=GAME_WARNING))
        else:
            marked.append(row)
    return marked


def _shader_warn_limit() -> int:
    """The smaller of the fixed limit and 5% of free space on the cache disk.

    Free space that cannot be read leaves the fixed limit in place: an unknown disk
    must not make every cache look large. A disk measured as full keeps a zero limit,
    so any measured cache still warns: that is when reclaiming space matters most.
    """
    try:
        free = diskspace.free_bytes(paths.cache_dir())
    except Exception:  # noqa: BLE001 - a card hint must never break the measurement
        log.debug("could not read the free space of Nadare's cache folder", exc_info=True)
        return TOTAL_WARN_BYTES
    share = int(free * TOTAL_WARN_SHARE)
    return min(TOTAL_WARN_BYTES, max(0, share))


def _total_warning(rows: Iterable[ShaderCache]) -> str:
    """One line when the measured caches together pass the limit; empty otherwise.

    Two profiles can share one prefix (and so one DXVK folder): the folder is on disk
    once, so its bytes count once even when the card shows a row per profile.
    """
    sizes: dict[str, int | None] = {}
    for index, row in enumerate(rows):
        if len(row.folder_sizes) == len(row.folders):
            for folder, size in zip(row.folders, row.folder_sizes, strict=True):
                key = os.path.normpath(str(folder))
                if key not in sizes or (sizes[key] is None and size is not None):
                    sizes[key] = size
        else:
            sizes[f"row:{index}"] = row.size
    total = sum(size for size in sizes.values() if size is not None)
    limit = _shader_warn_limit()
    if total <= limit:
        return ""
    return (
        f"Large: over {diskspace.human_size(limit)} of shader caches. "
        "Clearing is safe; games may stutter while they rebuild."
    )


def _shaders(profiles: Mapping[str, Any], probe: Callable[[Path], bool]) -> list[ShaderCache]:
    busy: dict[str, bool] = {}
    prefixes: dict[str, Path | None] = {}
    busy_prefixes: dict[str, Path | None] = {}
    titles: dict[str, str] = {}
    for name in sorted(profiles):
        profile = profiles.get(name)
        if not isinstance(profile, Mapping):
            continue
        prefixes[name] = _prefix(profile, name)
        busy_prefixes[name] = _prefix(profile, name, for_running=True)
        titles[name] = str(profile.get("title") or name)
        try:
            prefix = busy_prefixes[name]
            busy[name] = bool(probe(prefix)) if prefix is not None else False
        except Exception:  # noqa: BLE001
            busy[name] = True

    rows: list[ShaderCache] = []
    mesa = shadercache.mesa_dir()
    size = _cache_bytes(mesa) if _under_cache(mesa) else None
    if _real_dir(mesa) and _under_cache(mesa):
        rows.append(
            ShaderCache(
                key="mesa",
                label="Mesa",
                profile=None,
                folders=(mesa,),
                size=size,
                running=any(busy.values()),
                prefixes=tuple(p for p in busy_prefixes.values() if p is not None),
                folder_sizes=(size,),
            )
        )

    for name in sorted(prefixes):
        title = titles[name]
        vkd3d = shadercache.vkd3d_dir(name)
        size = _cache_bytes(vkd3d) if _under_cache(vkd3d) else None
        if _real_dir(vkd3d) and _under_cache(vkd3d):
            rows.append(
                ShaderCache(
                    key=f"vkd3d:{name}",
                    label=f"VKD3D · {title}",
                    profile=name,
                    folders=(vkd3d,),
                    size=size,
                    running=busy.get(name, False),
                    prefix=busy_prefixes[name],
                    folder_sizes=(size,),
                )
            )
        prefix = prefixes[name]
        if prefix is None:
            continue
        dvk = []
        for user in _users(prefix):
            folder = user / "AppData" / "Local" / "dxvk"
            if _is_dxvk(folder, prefix):
                dvk.append(folder)
        if not dvk:
            continue
        sizes = [_cache_bytes(folder) for folder in dvk]
        total = None if None in sizes else sum(size for size in sizes if size is not None)
        rows.append(
            ShaderCache(
                key=f"dxvk:{name}",
                label=f"DXVK · {title}",
                profile=name,
                folders=tuple(dvk),
                size=total,
                running=busy.get(name, False),
                prefix=prefix,
                folder_sizes=tuple(sizes),
            )
        )
    return _warn_games(rows)


def scan(
    profiles: Mapping[str, Any],
    *,
    steamapps: Iterable[str | os.PathLike[str]] | None = None,
    running: Callable[[Path], bool] | None = None,
) -> Report:
    """Measure every category; never raises (an unreadable part is simply empty)."""
    probe = running or is_running
    try:
        installers = _installers()
    except Exception:  # noqa: BLE001
        log.debug("could not list installed installers", exc_info=True)
        installers = Installers((), 0)
    try:
        temps = tuple(_temps(profiles, probe))
    except Exception:  # noqa: BLE001
        log.debug("could not measure game temporary folders", exc_info=True)
        temps = ()
    try:
        backups = tuple(_backups(steamapps))
    except Exception:  # noqa: BLE001
        log.debug("could not list Steam prefix backups", exc_info=True)
        backups = ()
    try:
        shaders = tuple(_shaders(profiles, probe))
    except Exception:  # noqa: BLE001
        log.debug("could not measure shader caches", exc_info=True)
        shaders = ()
    try:
        webcaches = tuple(_webcaches(profiles, probe))
    except Exception:  # noqa: BLE001
        log.debug("could not measure launcher web caches", exc_info=True)
        webcaches = ()
    try:
        shader_warning = _total_warning(shaders)
    except Exception:  # noqa: BLE001
        log.debug("could not compare the shader caches with the disk", exc_info=True)
        shader_warning = ""
    return Report(
        installers=installers,
        temps=temps,
        backups=backups,
        shaders=shaders,
        webcaches=webcaches,
        shader_warning=shader_warning,
    )


# ---- deleting --------------------------------------------------------------------------------


def _miss(result: ClearResult, path: Path, reason: str) -> None:
    result.kept.append(path)
    result.reason = reason


def _delete_folder(path: Path, result: ClearResult) -> None:
    """Delete one already-checked real folder; a mount or a stuck file stays."""
    try:
        # Open the folder (and its parent) first: a hostile process can swap the path
        # for a link while the size is measured, and the deletion must still happen in
        # the folder that was checked, not in whatever the name points at then.
        with removal.pinned_tree(path) as tree:
            size = diskspace.tree_bytes(tree.handle)
            outcome = tree.delete()
    except OSError as exc:
        log.warning("could not delete %s: %s", path, exc)
        _miss(result, path, f"could not delete {path}")
        return
    for mount in outcome.mounts:
        _miss(result, mount, f"{mount} is a mounted drive and was left alone")
    for left, exc in outcome.leftovers:
        log.warning("could not delete %s: %s", left, exc)
        _miss(result, left, f"could not delete {left}")
    if outcome.mounts or outcome.leftovers or path.exists():
        if path not in result.kept and not any(
            path == kept or path in kept.parents for kept in result.kept
        ):
            _miss(result, path, f"{path} was not empty after deleting")
        return
    result.removed.append(path)
    result.freed += size


def clear_installers(files: Iterable[str | os.PathLike[str]]) -> ClearResult:
    """Unlink only installer files the known cache folders currently list."""
    result = ClearResult()
    known = dict(installercache.installers())
    for given in files:
        path = Path(given)
        if path not in known:
            _miss(result, path, f"{path} is not an installer Nadare can delete")
            continue
        try:
            os.unlink(path)
        except OSError as exc:
            log.warning("could not delete the installer %s: %s", path, exc)
            _miss(result, path, f"could not delete {path}")
            continue
        result.removed.append(path)
        result.freed += known[path]
    return result


def _is_temp(path: Path, prefix: Path) -> bool:
    """``path`` is exactly ``<prefix>/drive_c/users/<user>/AppData/Local/Temp``."""
    try:
        if path.name != "Temp":
            return False
        parent = path.parent
        if parent.name != "Local" or parent.parent.name != "AppData":
            return False
        user = parent.parent.parent
        if user.parent.name != "users" or user.parent.parent.name != "drive_c":
            return False
        if user.parent.parent.parent != prefix:
            return False
        if prefix.is_symlink() or path.is_symlink() or not _real_dir(path):
            return False
        real = path.resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError):
        return False
    if real != root and root not in real.parents:
        return False
    current = path
    while current != prefix and current != current.parent:
        if current.is_symlink():
            return False
        current = current.parent
    return True


def clear_temp(entry: PrefixTemp) -> ClearResult:
    """Delete this game's prefix ``Temp`` folders; refuse while the game runs."""
    result = ClearResult()
    if entry.running or is_running(entry.prefix):
        for folder in entry.folders:
            _miss(result, folder, "the game is running, so its temporary files were kept")
        return result
    for folder in entry.folders:
        if not _is_temp(folder, entry.prefix):
            _miss(result, folder, f"{folder} is not this game's temporary folder")
            continue
        _delete_folder(folder, result)
        if folder in result.removed:
            # Wine does not recreate %TEMP% on every start; a game or installer would fail.
            try:
                folder.mkdir()
            except OSError as exc:
                log.warning("could not recreate %s: %s", folder, exc)
    return result


def _no_link_on_path(path: Path, top: Path) -> bool:
    """``path`` is below ``top`` and neither it nor any folder between is a link."""
    try:
        parts = path.relative_to(top).parts
    except ValueError:
        return False
    current = top
    try:
        if current.is_symlink():
            return False
        for part in parts:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _is_web_profile(profile: Path, prefix: Path) -> bool:
    """``profile`` is a browser profile under ``<prefix>/drive_c/users/<user>/AppData``."""
    try:
        relative = profile.relative_to(prefix)
        if profile.is_symlink() or not _real_dir(profile):
            return False
        parts = relative.parts
        if len(parts) < 6:
            return False
        if parts[0] != "drive_c" or parts[1] != "users" or parts[2] == "Public":
            return False
        if parts[3] != "AppData" or parts[4] not in ("Local", "Roaming"):
            return False
        depth = len(parts) - 5
        if not 1 <= depth <= WEB_PROFILE_DEPTH:
            return False
        if not _no_link_on_path(profile, prefix):
            return False
        real = profile.resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if real != root and root not in real.parents:
        return False
    return _looks_like_web_profile(profile)


def _is_web_cache(path: Path, profile: Path, prefix: Path) -> bool:
    """``path`` is exactly ``<profile>/<allowed cache name>`` inside this game's prefix."""
    if path not in _web_cache_paths(profile):
        return False
    if path.is_symlink() or not _real_dir(path):
        return False
    if not _is_web_profile(profile, prefix):
        return False
    try:
        real = path.resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError):
        return False
    if real != root and root not in real.parents:
        return False
    return _no_link_on_path(path, prefix)


def clear_webcache(entry: WebCache) -> ClearResult:
    """Empty only the allowed cache folders of this game's browser profiles.

    The profile's ``Cookies``, ``Local Storage``, ``Local State`` and the other files
    that keep the player signed in are never touched, and nothing is cleared while the
    game runs.
    """
    result = ClearResult()
    existing = [
        path
        for profile in entry.profiles
        for path in _web_cache_paths(profile)
        if os.path.lexists(path)
    ]
    if entry.running or is_running(entry.prefix):
        for path in existing:
            _miss(result, path, "the game is running, so its web cache was kept")
        return result
    for profile in entry.profiles:
        if not _is_web_profile(profile, entry.prefix):
            _miss(result, profile, f"{profile} is not a browser profile in this game's prefix")
            continue
        for path in _web_cache_paths(profile):
            if not os.path.lexists(path):
                continue
            if not _is_web_cache(path, profile, entry.prefix):
                _miss(result, path, f"{path} is not a web cache Nadare clears")
                continue
            _delete_folder(path, result)
            if path in result.removed:
                # The launcher expects its cache folders to exist on the next start.
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log.warning("could not recreate %s: %s", path, exc)
    return result


def _is_backup(path: Path, compatdata: Path) -> bool:
    try:
        return (
            compatdata.name == "compatdata"
            and compatdata.parent.name == "steamapps"
            and not compatdata.is_symlink()
            and path.parent == compatdata
            and bool(_BACKUP.match(path.name))
            and not path.is_symlink()
            and _real_dir(path)
        )
    except OSError:
        return False


def clear_backup(entry: Backup) -> ClearResult:
    """Delete one listed ``<appid>.bak-pre-*`` folder."""
    result = ClearResult()
    if not _is_backup(entry.path, entry.compatdata):
        _miss(result, entry.path, f"{entry.path} is not a Steam prefix backup")
        return result
    match = _BACKUP.match(entry.path.name)
    if match is None or match.group("app") != entry.app_id:
        _miss(result, entry.path, f"{entry.path} belongs to another game")
        return result
    try:
        live = entry.compatdata / entry.app_id
        in_use = live.resolve() == entry.path.resolve()
    except (OSError, RuntimeError):
        in_use = True
    if in_use or is_running(entry.path) or is_running(entry.path / "pfx"):
        _miss(result, entry.path, "the prefix is in use, so its backup was kept")
        return result
    _delete_folder(entry.path, result)
    return result


def _under_cache(path: Path) -> bool:
    """``path`` is below Nadare's cache dir and none of its components is a link."""
    try:
        relative = path.relative_to(paths.cache_dir())
    except ValueError:
        return False
    current = paths.cache_dir()
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _is_dxvk(path: Path, prefix: Path) -> bool:
    """``path`` is ``<prefix>/drive_c/users/<user>/AppData/Local/dxvk``."""
    try:
        if path.name != "dxvk":
            return False
        parent = path.parent
        if parent.name != "Local" or parent.parent.name != "AppData":
            return False
        user = parent.parent.parent
        if user.parent.name != "users" or user.parent.parent.name != "drive_c":
            return False
        if user.parent.parent.parent != prefix:
            return False
        if prefix.is_symlink() or path.is_symlink() or not _real_dir(path):
            return False
        real = path.resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError):
        return False
    if real != root and root not in real.parents:
        return False
    current = path
    while current != prefix and current != current.parent:
        if current.is_symlink():
            return False
        current = current.parent
    return True


def _safe_shader(entry: ShaderCache, folder: Path) -> bool:
    try:
        if folder.is_symlink():
            return False
    except OSError:
        return False
    if entry.key == "mesa":
        return folder == shadercache.mesa_dir() and _under_cache(folder)
    if entry.key.startswith("vkd3d:"):
        name = entry.key.split(":", 1)[1]
        try:
            expected = shadercache.vkd3d_dir(name)
        except ValueError:
            return False
        return folder == expected and _under_cache(folder)
    if entry.key.startswith("dxvk:"):
        return entry.prefix is not None and _is_dxvk(folder, entry.prefix)
    return False


def clear_shader(entry: ShaderCache) -> ClearResult:
    """Delete the listed shader cache folders; never NVIDIA's host cache."""
    result = ClearResult()
    if entry.running or any(is_running(prefix) for prefix in entry.prefixes):
        for folder in entry.folders:
            _miss(result, folder, "a game is running, so its shader cache was kept")
        return result
    if entry.profile is not None and entry.prefix is not None and is_running(entry.prefix):
        for folder in entry.folders:
            _miss(result, folder, "the game is running, so its shader cache was kept")
        return result
    for folder in entry.folders:
        if not _safe_shader(entry, folder):
            _miss(result, folder, f"{folder} is not a cache Nadare owns")
            continue
        _delete_folder(folder, result)
    return result


__all__ = [
    "Backup",
    "ClearResult",
    "Installers",
    "PrefixTemp",
    "Report",
    "ShaderCache",
    "WebCache",
    "clear_backup",
    "clear_installers",
    "clear_shader",
    "clear_temp",
    "clear_webcache",
    "is_running",
    "scan",
    "steamapps_dirs",
]
