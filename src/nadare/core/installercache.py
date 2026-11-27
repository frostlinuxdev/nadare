# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Installers in the cache are kept only while a library game needs them (#1417).

Nadare downloads Windows installers once and keeps them for the next prefix that needs
them: the WebView2 Runtime (~215 MB), the Visual C++ runtime (~25 MB per folder) and the
publishers' own launcher installers. Nothing removed them, even after every game that used
them had left the library, and each new launcher version stayed next to the old ones.

The rule (DECISIONS 2026-11-16): a cache folder's installers go when no game in the library
uses that folder; inside a folder still in use only the newest copy of each versioned
installer stays. Only installer files (``.exe``/``.msi``) older than a day, never through a
link. Everything here can be downloaded again.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, steamfix
from .config import as_bool

log = logging.getLogger(__name__)

#: An installer younger than this may be downloading for a game being added right now.
MIN_AGE = 24 * 3600

_INSTALLER_SUFFIXES = (".exe", ".msi")

#: Cache folder -> the backends whose games use it.
_FOLDER_BACKENDS: dict[str, frozenset[str]] = {
    # WebView2 (shared with GFL2 Haoplay and the Steam fixes) and CZN's VC++.
    "czn": frozenset({"stove-czn", "direct-gfl2-haoplay"}),
    "wine": frozenset({"wine"}),
    # WineGameBackend.cache_dir() is one folder for every Yostar/launcher kind.
    "arknights-yostar": frozenset(
        {
            "arknights-yostar",
            "stellasora-yostar",
            "hbr-yostar",
            "azurpromilia-jp-yostar",
            "launcher",
            "launcher-nikke",
        }
    ),
}

#: Wine-game kinds whose Visual C++ step is on by default (``vcredist_default``): their
#: installer sits in the shared Wine-game folder too (#1467). A test keeps this in step with
#: every backend class's own flag, so the cleanup cannot fall behind the producers again.
_VCREDIST_ON_BY_DEFAULT = frozenset({"direct-mongil"})
#: Where a Wine game's Visual C++ installer is kept (``WineGameBackend.cache_dir()``).
_WINE_GAME_FOLDER = "arknights-yostar"

#: A version inside a file name: ``nikkeminiloader0.0.6.346.exe`` -> ``0.0.6.346``.
_VERSION = re.compile(r"\d+(?:[._-]\d+)+")


@dataclass
class Result:
    removed: list[Path] = field(default_factory=list)
    freed: int = 0


def _setting(profile: Mapping[str, Any], key: str) -> Any:
    value = profile.get(key)
    if value in (None, ""):
        extra = profile.get("extra")
        if isinstance(extra, Mapping):
            value = extra.get(key)
    return value


def _steam_needs_webview2(profile: Mapping[str, Any]) -> bool:
    app_id = _setting(profile, "app_id")
    row = steamfix.fix_for(str(app_id)) if app_id not in (None, "") else None
    return row is not None and steamfix.KEY_WEBVIEW2 in row.needs


def needed_folders(profiles: Mapping[str, Any]) -> set[str]:
    """The cache folders whose installers a game in ``profiles`` may need again."""
    needed: set[str] = set()
    for profile in profiles.values():
        if not isinstance(profile, Mapping):
            continue
        backend = str(profile.get("backend") or "")
        for folder, users in _FOLDER_BACKENDS.items():
            if backend in users:
                needed.add(folder)
        if backend == "steam" and _steam_needs_webview2(profile):
            needed.add("czn")
        # The same rule the launch uses: on by the class, or switched on in the profile (#1467).
        if backend in _VCREDIST_ON_BY_DEFAULT or as_bool(_setting(profile, "vcredist"), False):
            needed.add(_WINE_GAME_FOLDER)
    return needed


def _installers(folder: Path) -> list[tuple[Path, os.stat_result]]:
    """Regular installer files directly in a real (not linked) ``folder``."""
    try:
        st = folder.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(st.st_mode):
        return []
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if name.startswith(".") or not name.lower().endswith(_INSTALLER_SUFFIXES):
            continue
        path = folder / name
        try:
            fst = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(fst.st_mode):
            found.append((path, fst))
    return found


def _remove(path: Path, st: os.stat_result, result: Result) -> None:
    try:
        os.unlink(path)
    except OSError as exc:
        log.debug("could not remove the installer %s: %s", path, exc)
        return
    result.removed.append(path)
    result.freed += st.st_size
    log.info("removed an installer no game needs any more: %s (%d bytes)", path, st.st_size)


def _older_versions(
    files: list[tuple[Path, os.stat_result]],
) -> list[tuple[Path, os.stat_result]]:
    """Every versioned installer but the newest of its name."""
    groups: dict[str, list[tuple[Path, os.stat_result]]] = {}
    for path, st in files:
        groups.setdefault(_VERSION.sub("#", path.name.lower()), []).append((path, st))
    older = []
    for members in groups.values():
        members.sort(key=lambda item: item[1].st_mtime)
        older.extend(members[:-1])
    return older


def installers(root: Path | None = None) -> list[tuple[Path, int]]:
    """Every installer file in the known cache folders, with its size.

    The list is what the Settings "Disk space" card shows: regular ``.exe``/``.msi``
    files directly in the folders this module owns, never a hidden run copy and never
    through a link.
    """
    base = paths.cache_dir() if root is None else Path(root)
    found: list[tuple[Path, int]] = []
    if base.is_symlink():
        return found
    for folder in _FOLDER_BACKENDS:
        for path, st in _installers(base / folder):
            found.append((path, st.st_size))
    return found


def sweep(
    profiles: Mapping[str, Any],
    *,
    now: float | None = None,
    min_age: float = MIN_AGE,
) -> Result:
    """Remove installers no library game needs; never raises."""
    result = Result()
    when = time.time() if now is None else now
    try:
        needed = needed_folders(profiles)
        root = paths.cache_dir()
        if root.is_symlink():
            return result
        for folder in _FOLDER_BACKENDS:
            files = _installers(root / folder)
            pick: Callable[[list[tuple[Path, os.stat_result]]], list] = (
                _older_versions if folder in needed else list
            )
            for path, st in pick(files):
                if when - st.st_mtime >= min_age:
                    _remove(path, st, result)
    except Exception:  # noqa: BLE001 - a start-up tidy-up must never break start-up
        log.debug("installer cache sweep failed", exc_info=True)
    return result


__all__ = ["MIN_AGE", "Result", "installers", "needed_folders", "sweep"]
