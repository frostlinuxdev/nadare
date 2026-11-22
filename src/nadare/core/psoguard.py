# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep a game's shader (pipeline) cache from being lost to a cut-short save (#1421).

Arknights: Endfield compiles its Vulkan pipelines for minutes the first time and keeps them in
``vulkan_pso_cache.bin`` (Unity's 32-byte header, then the driver's ``VkPipelineCache`` with the
GPU vendor, device and ``pipelineCacheUUID``). On 2026-11-15 a 38 s session that still ended with
exit 0 left a 1 MB file over the 189 MB one; the next start rejected it and compiled everything
again. Nadare cannot change how the game saves, so it keeps a copy:

* after a session that changed the cache, a sane cache is copied to the profile's data folder
  (outside the prefix, so a rebuilt prefix keeps it);
* when the cache shrank below half of that copy while the GPU, device and driver identity stayed
  the same, the save was cut short: the copy goes back. A new driver or GPU changes the identity,
  and then a smaller fresh cache is right;
* when the cache is missing at Play and a copy exists, the copy goes back.

Copies are written to a temporary file, synced and renamed, so either the whole copy is there or
the old one is. Links are never followed. Nothing here may break a launch: errors are logged.
"""

from __future__ import annotations

import logging
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import diskspace

log = logging.getLogger("nadare.psoguard")

#: A kept copy smaller than this is never put back: shrinking is ordinary churn there.
MIN_KEPT_BYTES = 16 * 1024 * 1024
#: A cache that shrank below this share of the kept copy was cut short (same identity).
SHRINK_RATIO = 0.5

_UNITY_HEADER = 32
_VULKAN_HEADER = struct.Struct("<4I16s")  # headerSize, headerVersion, vendorID, deviceID, UUID


def wine_user_dir(prefix: Path) -> Path:
    """``drive_c/users/steamuser`` (Proton), else the prefix's one real user folder."""
    users = prefix / "drive_c" / "users"
    proton = users / "steamuser"
    if proton.is_dir() and not proton.is_symlink():
        return proton
    try:
        for entry in sorted(users.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and entry.name != "Public":
                return entry
    except OSError:
        pass
    return proton


def endfield_cache(prefix: Path) -> Path:
    """Where Endfield keeps its pipeline cache inside ``prefix``."""
    return (
        wine_user_dir(prefix)
        / "AppData"
        / "LocalLow"
        / "Gryphline"
        / "Endfield"
        / "vulkan_pso_cache.bin"
    )


@dataclass(frozen=True)
class Stamp:
    size: int
    mtime_ns: int
    #: Vendor, device and pipelineCacheUUID from the Vulkan header; ``None`` for another shape.
    identity: bytes | None


def _identity(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(_UNITY_HEADER + _VULKAN_HEADER.size)
    except OSError:
        return None
    if len(head) < _UNITY_HEADER + _VULKAN_HEADER.size:
        return None
    size, version, _vendor, _device, _uuid = _VULKAN_HEADER.unpack_from(head, _UNITY_HEADER)
    if size != _VULKAN_HEADER.size or version != 1:
        return None
    return head[_UNITY_HEADER + 8 :]  # vendor + device + UUID


def stamp(path: Path) -> Stamp | None:
    """Size, time and identity of a regular file (a link is never read)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    return Stamp(info.st_size, info.st_mtime_ns, _identity(path))


def _copy_into(source: Path, destination: Path) -> None:
    """Copy ``source`` over ``destination`` whole or not at all (temp file, fsync, rename)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.nadare-{os.getpid()}.tmp")
    try:
        with open(source, "rb") as reader, open(temp, "wb") as writer:
            while True:
                chunk = reader.read(4 * 1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    try:
        fd = os.open(destination.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _cut_short(now: Stamp, kept: Stamp | None) -> bool:
    return (
        kept is not None
        and kept.size >= MIN_KEPT_BYTES
        and now.size < kept.size * SHRINK_RATIO
        and now.identity is not None
        and now.identity == kept.identity
    )


class Guard:
    """One Play: :meth:`before` ahead of the game, :meth:`after` once it has ended."""

    def __init__(
        self,
        cache: Path,
        backup_dir: Path,
        say: Callable[[str], None],
        *,
        home: Path | None = None,
    ) -> None:
        """``home``: a missing cache is put back only when this folder exists (the prefix's
        Wine user folder): never into a prefix Proton has not set up yet."""
        self.cache = cache
        self.home = home
        self.kept = backup_dir / cache.name
        self._say = say
        self._before: Stamp | None = None

    def _sweep_temps(self) -> None:
        try:
            entries = list(self.kept.parent.iterdir())
        except OSError:
            return
        prefix = f".{self.kept.name}.nadare-"
        for entry in entries:
            if entry.name.startswith(prefix) and entry.name.endswith(".tmp"):
                entry.unlink(missing_ok=True)

    def before(self) -> None:
        try:
            self._sweep_temps()
            kept = stamp(self.kept)
            ready = self.home is None or (self.home.is_dir() and not self.home.is_symlink())
            if kept is not None and ready and not os.path.lexists(self.cache):
                _copy_into(self.kept, self.cache)
                self._say(
                    "shader cache: put the last good copy back "
                    f"({diskspace.human_size(kept.size)}), so the game skips its long warm-up"
                )
            self._before = stamp(self.cache)
        except OSError:
            log.warning("shader cache check before Play failed", exc_info=True)

    def after(self) -> None:
        try:
            now = stamp(self.cache)
            if now is None or now == self._before:
                return  # nothing written, or a link or folder where the file should be
            kept = stamp(self.kept)
            if kept is not None and _cut_short(now, kept):
                _copy_into(self.kept, self.cache)
                self._say(
                    f"shader cache: the game's save was cut short "
                    f"({diskspace.human_size(now.size)}, was {diskspace.human_size(kept.size)}); "
                    "put the last good copy back, so the next start skips the long warm-up"
                )
                return
            if now.identity is None:
                return  # not a shape we know: keep the old copy, never judge this one
            if os.path.islink(self.kept):
                self.kept.unlink()
            _copy_into(self.cache, self.kept)
        except OSError:
            log.warning("shader cache check after Play failed", exc_info=True)


__all__ = ["Guard", "Stamp", "endfield_cache", "stamp", "wine_user_dir"]
