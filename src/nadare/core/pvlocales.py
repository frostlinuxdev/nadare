# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep the locales the Steam Runtime container would compile on every launch.

umu starts every Proton game in pressure-vessel's container, which wants ``en_US.UTF-8`` (and the
user's own locale). When the host has not generated one of them (a British or German desktop,
say), ``pv-locale-gen`` compiles them into a throw-away folder on **every** launch: 1.6-2 s of
each Play (Endfield: 15.5 s -> 13.5 s to the window).

Nadare runs the same tool once, keeps its output under the Sandbox's cache folder (visible to the
game with or without the Sandbox), and passes it on with ``LOCPATH`` and
``PRESSURE_VESSEL_GENERATE_LOCALES=0``: the container loads the same files it would have built.

The files are built with the host's ``localedef``; they are only used when the host's glibc is at
least the runtime's, because only then does the container run the host's glibc. Anything unknown
or failing leaves pressure-vessel to do what it always did.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOCALE_VARS = (
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_ADDRESS",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_IDENTIFICATION",
    "LC_MEASUREMENT",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NAME",
    "LC_NUMERIC",
    "LC_PAPER",
    "LC_TELEPHONE",
    "LC_TIME",
)
#: Where the folders live inside the cache folder the Sandbox shares with the game.
FOLDER = "pv-locales"
#: ``pv-locale-gen`` exits 72 when it generated locales (``LOCPATH`` should point there), 0 when
#: nothing was missing.
_GENERATED = 72
_NOTHING_MISSING = 0
_TIMEOUT = 90
_NONE_MARK = ".none"
_TOOL_PARTS = ("pressure-vessel", "libexec", "steam-runtime-tools-0", "pv-locale-gen")
_LIBC_PARTS = ("files", "lib", "x86_64-linux-gnu", "libc.so.6")
_VERSION_RE = re.compile(rb"release version (\d+)\.(\d+)")
_RUNTIME_GLIBC: dict[tuple[str, int], tuple[int, int] | None] = {}


def _in_flatpak() -> bool:
    from . import sandbox

    return sandbox.in_flatpak()


def _umu_roots() -> list[Path]:
    """Owned and shared umu data folders (including old installs)."""
    from . import umu

    return umu.runtime_roots()


def _newest(paths: list[Path]) -> Path | None:
    found = []
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(found)[1] if found else None


def locale_gen_tool() -> Path | None:
    """The newest ``pv-locale-gen`` umu downloaded, or ``None``."""
    pattern = "*/" + "/".join(_TOOL_PARTS)
    return _newest([found for root in _umu_roots() for found in root.glob(pattern)])


def runtime_glibc() -> tuple[int, int] | None:
    """The newest glibc among the runtimes umu downloaded (read from ``libc.so.6``)."""
    best: tuple[int, int] | None = None
    for root in _umu_roots():
        for libc in root.glob("*/*/" + "/".join(_LIBC_PARTS)):
            try:
                key = (str(libc), libc.stat().st_mtime_ns)
            except OSError:
                continue
            if key not in _RUNTIME_GLIBC:
                version = None
                with contextlib.suppress(OSError):
                    found = _VERSION_RE.search(libc.read_bytes())
                    if found:
                        version = (int(found.group(1)), int(found.group(2)))
                _RUNTIME_GLIBC[key] = version
            version = _RUNTIME_GLIBC[key]
            if version is not None and (best is None or version > best):
                best = version
    return best


def host_glibc() -> tuple[int, int] | None:
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return None
    found = re.search(r"(\d+)\.(\d+)", text)
    return (int(found.group(1)), int(found.group(2))) if found else None


def _key(env: Mapping[str, str], tool: Path, host: tuple[int, int], rt: tuple[int, int]) -> str:
    try:
        info = tool.stat()
        stamp = [info.st_size, info.st_mtime_ns]
    except OSError:
        stamp = []
    locale = {name: env[name] for name in LOCALE_VARS if env.get(name)}
    raw = json.dumps([list(host), list(rt), str(tool), stamp, locale], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _kept(folder: Path) -> dict[str, str] | None:
    """What a kept folder means for the launch; ``None`` when it is not a usable folder."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return None
        if (folder / _NONE_MARK).is_file():
            return {"PRESSURE_VESSEL_GENERATE_LOCALES": "0"}
        if any(child.is_dir() and not child.is_symlink() for child in folder.iterdir()):
            return {"LOCPATH": str(folder), "PRESSURE_VESSEL_GENERATE_LOCALES": "0"}
    except OSError:
        return None
    return None


def prepare(
    env: Mapping[str, str],
    cache_dir: str | os.PathLike[str],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, str]:
    """Variables to add to a umu launch's environment; ``{}`` leaves pressure-vessel alone."""
    if env.get("LOCPATH") or "PRESSURE_VESSEL_GENERATE_LOCALES" in env or _in_flatpak():
        return {}
    tool = locale_gen_tool()
    host = host_glibc()
    rt = runtime_glibc()
    if tool is None or host is None or rt is None or host < rt:
        return {}
    base = Path(cache_dir) / FOLDER
    folder = base / _key(env, tool, host, rt)
    kept = _kept(folder)
    if kept is not None:
        return kept
    with contextlib.suppress(OSError):
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder)
    try:
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".new-", dir=base))
    except OSError as exc:
        log.debug("pv-locales: no cache folder (%s)", exc)
        return {}
    try:
        tool_env = {name: env[name] for name in (*LOCALE_VARS, "PATH") if env.get(name)}
        tool_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        try:
            done = run(
                [str(tool), "--output-dir", str(work)],
                env=tool_env,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("pv-locales: could not run %s (%s)", tool, exc)
            return {}
        made = [p for p in work.iterdir() if p.is_dir() and not p.is_symlink()]
        if done.returncode == _NOTHING_MISSING and not made:
            (work / _NONE_MARK).write_text("nothing missing\n")
        elif done.returncode != _GENERATED or not made:
            log.info("pv-locales: %s exited %s; pressure-vessel does it", tool, done.returncode)
            return {}
        os.chmod(work, 0o755)
        try:
            work.rename(folder)
        except OSError:
            return _kept(folder) or {}  # another launch kept it first
        work = folder
        log.info("pv-locales: kept %s", folder)
        return _kept(folder) or {}
    finally:
        if work != folder:
            shutil.rmtree(work, ignore_errors=True)
