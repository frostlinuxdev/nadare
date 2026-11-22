# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Owned shader cache paths and bounded, read-only storage diagnostics.

Mesa is shared by every game (``<root>/mesa``); VKD3D is per profile
(``<root>/games/<profile>/vkd3d``); DXVK stays in the prefix; NVIDIA's driver
cache is only reported, never moved or cleared. Mesa keeps its own default size
limit: no ``MESA_SHADER_CACHE_MAX_SIZE`` is set. Sizes are logical bytes, or
``None`` when the bounded scan did not finish.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import atomicfile, paths
from .config import valid_profile_name

log = logging.getLogger(__name__)

#: Sizes in the launch log are best effort: one layer may spend this long and no more.
SCAN_SECONDS = 0.05
#: Entries one layer may visit before its size is reported as unknown.
SCAN_ENTRIES = 20_000

#: Remembered beside a profile's other data: the runner versions its last Play used.
VERSIONS_FILE = "shader-versions.json"
#: A record past this size is no baseline, not something to read into memory.
VERSIONS_MAX_BYTES = 64 * 1024
#: Proton trees record the builds they ship in these files (one text line each).
_RUNNER_VERSION_FILES: dict[str, tuple[str, ...]] = {
    "DXVK": (
        "files/lib/wine/dxvk/version",
        "files/lib64/wine/dxvk/version",
        "lib/wine/dxvk/version",
        "lib64/wine/dxvk/version",
        "dist/lib/wine/dxvk/version",
        "dist/lib64/wine/dxvk/version",
    ),
    "VKD3D": (
        "files/lib/wine/vkd3d-proton/version",
        "files/lib64/wine/vkd3d-proton/version",
        "lib/wine/vkd3d-proton/version",
        "lib64/wine/vkd3d-proton/version",
        "dist/lib/wine/vkd3d-proton/version",
        "dist/lib64/wine/vkd3d-proton/version",
    ),
}


def mesa_dir() -> Path:
    return paths.shader_cache_dir() / "mesa"


def _ensure_owned_dir(folder: Path) -> None:
    """Create ``folder`` below Nadare's cache dir without following a link on the way.

    A Sandboxed game can replace anything inside ``sandbox-cache`` (including
    ``shader-cache/games``) with a link to a host folder. Every component is created
    and reopened relative to one open directory descriptor, with ``O_NOFOLLOW``, so a
    link is rejected instead of leading the creation outside the cache.
    """
    base = paths.cache_dir()
    # This also keeps the old "cannot write to the cache" message when the cache root
    # itself is read-only; a link at the root is rejected by the walk below.
    paths.shader_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        relative = folder.relative_to(base)
    except ValueError:
        raise OSError(f"{folder} is outside the cache folder {base}") from None
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)


def vkd3d_dir(profile: str) -> Path:
    if not valid_profile_name(profile):
        raise ValueError("invalid profile name")
    return paths.shader_cache_dir() / "games" / profile / "vkd3d"


def prepare(
    profile: str,
    env: dict[str, str],
    info: Callable[[str], None],
    *,
    runner: Path | str | None = None,
) -> None:
    """Apply the owned defaults before the profile's environment is merged.

    A value inherited from the desktop session is replaced (and logged); a value
    the profile sets itself is merged later and wins. A folder that cannot be
    created or written does not stop the launch: the game then uses whatever the
    driver falls back to, and the log says so.

    ``runner``: this Play's Proton tree, when it has one. A runner that ships a
    different DXVK or VKD3D build than the last Play gets one note (see
    :func:`note_runner_change`).
    """
    for key, folder in (
        ("MESA_SHADER_CACHE_DIR", mesa_dir()),
        ("VKD3D_SHADER_CACHE_PATH", vkd3d_dir(profile)),
    ):
        if key in env and env[key] != str(folder):
            info(f"shader cache: replacing host {key} with {folder}")
        env[key] = str(folder)
        try:
            _ensure_owned_dir(folder)
            if not os.access(folder, os.W_OK):
                info(f"shader cache: cannot write to {folder}")
        except OSError as exc:
            info(f"shader cache: could not create {folder}: {exc}")
    if runner is not None:
        try:
            note_runner_change(profile, Path(runner), info)
        except Exception:  # noqa: BLE001 - a note must never stop a launch
            log.warning("could not compare this runner's shader versions", exc_info=True)


def runner_versions(runner: Path) -> dict[str, str]:
    """The DXVK and VKD3D-Proton builds a Proton tree records, when it records them."""
    found: dict[str, str] = {}
    for layer, relatives in _RUNNER_VERSION_FILES.items():
        for relative in relatives:
            text = _version_text(Path(runner) / relative)
            if text:
                found[layer] = text
                break
    return found


def _version_text(path: Path) -> str | None:
    """The first line of a small regular file; a link or anything odd is ``None``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            text = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else None


def _read_versions(path: Path) -> dict[str, str]:
    """The remembered versions, or none when the record is absent or not a small file.

    The record sits in the profile's data folder, so treat it like any other file
    there: never follow a link, and never let a FIFO block a launch (a plain read
    would wait for a writer forever).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        raw = os.read(fd, VERSIONS_MAX_BYTES + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    if len(raw) > VERSIONS_MAX_BYTES:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        layer: value
        for layer, value in payload.items()
        if layer in _RUNNER_VERSION_FILES and isinstance(value, str) and value
    }


def note_runner_change(
    profile: str,
    runner: Path,
    info: Callable[[str], None],
    *,
    data_dir: Path | None = None,
) -> None:
    """One line when this runner's DXVK or VKD3D build differs from the last Play.

    The first Play records a baseline silently.  A later version change is said once,
    then the new versions become the baseline.  A tree without the version files says
    nothing and leaves the record alone (a file that appeared later would otherwise
    look like a change).  Nothing here may stop a launch.
    """
    if not valid_profile_name(profile):
        return
    versions = runner_versions(Path(runner))
    if not versions:
        return
    folder = Path(data_dir) if data_dir is not None else paths.profiles_dir() / profile
    record = folder / VERSIONS_FILE
    previous = _read_versions(record)
    changed = [
        layer
        for layer in ("DXVK", "VKD3D")
        if layer in versions and layer in previous and previous[layer] != versions[layer]
    ]
    if changed:
        names = " and ".join(changed)
        plural = "s" if len(changed) > 1 else ""
        info(
            f"New {names} version{plural}: "
            "the game may stutter while its shaders rebuild."
        )
    merged = {**previous, **versions}
    if merged != previous:
        try:
            atomicfile.write(
                record, json.dumps(merged, sort_keys=True), mode=0o600, make_parent=True
            )
        except OSError:
            log.warning("could not remember this runner's shader versions", exc_info=True)


def cache_size(
    folder: Path, *, max_seconds: float = SCAN_SECONDS, max_entries: int = SCAN_ENTRIES
) -> int | None:
    """Bound traversal by time and entries; never follow links or cross devices.

    The time budget is checked between filesystem calls; an individual slow
    filesystem operation cannot be interrupted. A missing folder counts as zero,
    anything the walk could not finish counts as ``None`` (unknown).
    """
    deadline = time.monotonic() + max_seconds
    total = 0
    visited = 0
    pending = [folder]
    try:
        root = folder.lstat()
        if stat.S_ISLNK(root.st_mode):
            return None
        while pending:
            if time.monotonic() >= deadline or visited >= max_entries:
                return None
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline or visited >= max_entries:
                        return None
                    visited += 1
                    meta = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(meta.st_mode):
                        if meta.st_dev != root.st_dev:
                            return None
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(meta.st_mode):
                        total += meta.st_size
        return total
    except FileNotFoundError:
        return 0 if not folder.exists() else None
    except OSError:
        return None


def _inside(path: Path, anchor: Path) -> bool:
    try:
        path.relative_to(anchor)
    except ValueError:
        return False
    return True


def _no_link_below(path: Path, anchor: Path) -> bool:
    """Whether no folder between ``anchor`` and ``path`` is a link.

    A path outside ``anchor`` is not this anchor's to check, so it passes here.
    """
    try:
        parts = path.relative_to(anchor).parts
    except ValueError:
        return True
    current = anchor
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _layer_size(folder: Path, anchor: Path) -> int | None:
    """The bounded size of ``folder``, or ``None`` when the way to it goes through a link."""
    if _inside(folder, anchor) and not _no_link_below(folder, anchor):
        return None
    return cache_size(folder)


def cache_layers(
    profile: str,
    prefix: Path,
    env: Mapping[str, str],
    *,
    sandboxed: bool = False,
) -> dict[str, tuple[Path | None, int | None]]:
    """Return layer -> (effective path, bytes) for the launch log and storage views.

    Proton uses ``steamuser``; plain Wine's user is the desktop user (``USER``).
    No prefix-wide scan is needed. NVIDIA is reported only: the Sandbox keeps its
    own copy under the sandbox cache, the host cache is never touched.
    An explicitly empty driver override has an unknown location; do not scan cwd
    or claim that the driver uses Nadare's default.
    """
    user = "steamuser" if env.get("PROTONPATH") else env.get("USER", "steamuser")
    # A malformed host username must not turn the diagnostic into an arbitrary scan.
    if user in (".", "..") or "/" in user or "\\" in user:
        user = "steamuser"
    nvidia_root = (
        paths.cache_dir() / "sandbox-cache"
        if sandboxed
        else Path(env.get("XDG_CACHE_HOME") or paths.cache_dir().parent)
    )
    folders: dict[str, Path | None] = {
        "Mesa": Path(env.get("MESA_SHADER_CACHE_DIR", str(mesa_dir()))),
        "VKD3D": Path(env.get("VKD3D_SHADER_CACHE_PATH", str(vkd3d_dir(profile)))),
        "DXVK": prefix / "drive_c/users" / user / "AppData/Local/dxvk",
        "NVIDIA": nvidia_root / "nvidia",
    }
    for layer, key in (("Mesa", "MESA_SHADER_CACHE_DIR"), ("VKD3D", "VKD3D_SHADER_CACHE_PATH")):
        if env.get(key) == "":
            folders[layer] = None
    anchors = {
        "Mesa": paths.cache_dir(),
        "VKD3D": paths.cache_dir(),
        "DXVK": prefix,
        "NVIDIA": nvidia_root,
    }
    return {
        layer: (folder, _layer_size(folder, anchors[layer]) if folder is not None else None)
        for layer, folder in folders.items()
    }


def log_caches(
    profile: str,
    prefix: Path,
    env: Mapping[str, str],
    info: Callable[[str], None],
    *,
    sandboxed: bool = False,
) -> None:
    """One launch-log line: the resolved path of every layer and its bounded size."""
    layers = cache_layers(profile, prefix, env, sandboxed=sandboxed)
    info(
        "shader caches: "
        + "; ".join(
            f"{layer}={folder if folder is not None else 'unknown'} "
            f"({size if size is not None else 'unknown'} bytes)"
            for layer, (folder, size) in layers.items()
        )
    )
