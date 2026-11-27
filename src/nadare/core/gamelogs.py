# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Find a game's own logs, so "Save logs as zip" can carry them (2026-11-26).

Nadare's launch log shows what Nadare and the runner printed. Why a game stopped is often only
in files the game, Proton or DXVK wrote themselves:

- Unity games write under ``AppData/LocalLow/<company>/<product>/`` in the prefix;
- Unreal games write ``Saved/Logs/*.log`` in the install or under ``AppData/Local/<game>/``;
- ``PROTON_LOG=1`` writes ``steam-<id>.log`` and ``DXVK_LOG_PATH`` the ``<exe>_d3d11.log``
  family, both into the prefix folder when a user points them there.

The search is generic, so every game is covered without a per-game list. It never follows a
symlink, looks only at ``.log`` files written in the last week, stops after a fixed number of
folder entries (an install can hold 85,000 files) and keeps only the newest few.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import location

MAX_FILES = 8
MAX_AGE_DAYS = 7
MAX_ENTRIES = 20_000
#: How deep below LocalLow a Unity log sits (``Company/Product/log/x.log`` is 4).
_LOCALLOW_DEPTH = 4
#: How deep in the install an Unreal ``Saved/Logs`` folder is looked for (``Client/Saved/Logs``).
_INSTALL_DEPTH = 4


@dataclass(frozen=True)
class GameLog:
    """One log file a game wrote, and the name it gets in the zip."""

    path: Path
    name: str
    mtime: float


class _Budget:
    def __init__(self, entries: int) -> None:
        self.left = entries

    def spend(self) -> bool:
        self.left -= 1
        return self.left >= 0


def _entries(folder: Path, budget: _Budget) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not budget.spend():
                    return
                yield entry
    except OSError:
        return


def _is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _log_file(entry: os.DirEntry[str], oldest: float) -> float | None:
    """The file's mtime when it is a fresh, regular ``.log`` (never through a symlink)."""
    if not entry.name.lower().endswith(".log"):
        return None
    try:
        if not entry.is_file(follow_symlinks=False):
            return None
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return None
    return mtime if mtime >= oldest else None


def _walk_logs(
    root: Path, depth: int, budget: _Budget, oldest: float
) -> Iterator[tuple[Path, float]]:
    """Every fresh ``.log`` file up to ``depth`` folders below ``root``."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and budget.left > 0:
        folder, level = stack.pop()
        for entry in _entries(folder, budget):
            if _is_dir(entry):
                if level < depth:
                    stack.append((Path(entry.path), level + 1))
                continue
            mtime = _log_file(entry, oldest)
            if mtime is not None:
                yield Path(entry.path), mtime


def _unreal_logs(
    root: Path, depth: int, budget: _Budget, oldest: float
) -> Iterator[tuple[Path, float]]:
    """The ``.log`` files of every ``Saved/Logs`` folder up to ``depth`` folders below ``root``."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and budget.left > 0:
        folder, level = stack.pop()
        for entry in _entries(folder, budget):
            if not _is_dir(entry):
                continue
            here = Path(entry.path)
            if entry.name == "Saved":
                logs = here / "Logs"
                if logs.is_dir() and not logs.is_symlink():
                    for item in _entries(logs, budget):
                        mtime = _log_file(item, oldest)
                        if mtime is not None:
                            yield Path(item.path), mtime
                continue
            if level < depth:
                stack.append((here, level + 1))


def _users(prefix: Path, budget: _Budget) -> Iterator[Path]:
    drive = prefix / "drive_c"
    if not drive.is_dir() and (prefix / "pfx" / "drive_c").is_dir():
        drive = prefix / "pfx" / "drive_c"  # a Proton compatdata folder (#1875 review)
    for entry in _entries(drive / "users", budget):
        if _is_dir(entry) and entry.name.lower() != "public":
            yield Path(entry.path)


def _named(path: Path, base: Path, label: str) -> str:
    return f"{label}/{path.relative_to(base).as_posix()}"


def find(
    install: Path | None,
    prefix: Path | None,
    *,
    now: float | None = None,
    max_files: int = MAX_FILES,
    max_age_days: float = MAX_AGE_DAYS,
    max_entries: int = MAX_ENTRIES,
) -> list[GameLog]:
    """The newest logs the game, Proton and DXVK wrote for one game, newest first."""
    oldest = (time.time() if now is None else now) - max_age_days * 86400
    budget = _Budget(max_entries)
    found: dict[str, GameLog] = {}

    def add(path: Path, name: str, mtime: float) -> None:
        kept = found.get(name)
        if kept is None or mtime > kept.mtime:
            found[name] = GameLog(path, name, mtime)

    if prefix is not None and prefix.is_dir():
        for entry in _entries(prefix, budget):  # steam-*.log, <exe>_d3d11.log, ...
            mtime = _log_file(entry, oldest)
            if mtime is not None:
                add(Path(entry.path), f"prefix/{entry.name}", mtime)
        for user in _users(prefix, budget):
            appdata = user / "AppData"
            low = appdata / "LocalLow"
            if low.is_dir() and not low.is_symlink():
                for path, mtime in _walk_logs(low, _LOCALLOW_DEPTH, budget, oldest):
                    add(path, _named(path, appdata, "prefix/AppData"), mtime)
            local = appdata / "Local"
            if local.is_dir() and not local.is_symlink():
                for path, mtime in _unreal_logs(local, 1, budget, oldest):
                    add(path, _named(path, appdata, "prefix/AppData"), mtime)
    if install is not None and install.is_dir():
        for path, mtime in _unreal_logs(install, _INSTALL_DEPTH, budget, oldest):
            add(path, _named(path, install, "install"), mtime)

    newest = sorted(found.values(), key=lambda item: item.mtime, reverse=True)
    return newest[:max_files]


#: A key that names an account (``uid: 123``, ``uid=123``, ``"accountId":"123"``). SDK logs print
#: these freely; the usual redaction only knows tokens and passwords.
_ACCOUNT_VALUE = re.compile(
    r"""(?ix)
    (\b(?:[a-z_-]{0,24}?(?:uid|guid|hgid|open[_-]?id|user[_-]?id|account[_-]?id|device[_-]?id|
          unique[_-]?id|unique[_-]?identifier|distinct[_-]?id|player[_-]?id|role[_-]?id)|
          user[_-]?name|nick[_-]?name|phone|mobile)\b"?\s*[:=]\s*"?)
    ([^\s",}\]]+)
    """
)
#: A JSON web token anywhere in a line.
_JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")


def mask(text: str) -> str:
    """Hide account names and ids, and JSON web tokens, in a log the game wrote itself."""
    out = _ACCOUNT_VALUE.sub(lambda m: f"{m.group(1)}<redacted>", text)
    return _JWT.sub("<redacted>", out)


def folders(
    profile: Mapping[str, Any], *, name: str | None = None
) -> tuple[Path | None, Path | None]:
    """The game's install and prefix folders from its profile (``None`` when unset).

    With ``name`` a profile that never set its prefix gets the one its backend uses by default
    (``<profiles>/<name>/prefix``), when that folder exists (#1875 review).
    """

    def path(*keys: str) -> Path | None:
        for key in keys:
            value = location._setting(profile, key)
            if value:
                return Path(os.path.expanduser(value))
        return None

    prefix = path("prefix", "wineprefix")
    if prefix is None and name:
        from . import backends, paths

        default = backends.wine_prefix(profile, paths.profiles_dir() / name)
        prefix = default if default.is_dir() else None
    return path("install_dir"), prefix


__all__ = ["MAX_FILES", "GameLog", "find", "folders", "mask"]
