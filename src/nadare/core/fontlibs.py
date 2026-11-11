# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Use the host's fontconfig and freetype in the AppImage, the bundled copies only as a fallback.

The AppImage is built on Ubuntu 22.04, whose fontconfig 2.13 cannot parse the ``/etc/fonts``
syntax of current distributions: it prints a wall of errors (Arch, CachyOS) and may resolve font
families differently from the desktop (#669). The host's own library understands its own config.
Qt links fontconfig directly, so a host without it (a minimal container) must still start: the
build moves the bundled copies into ``fallback-libs`` and this module loads one copy of each,
host first, with ``RTLD_GLOBAL`` before Qt is imported. Qt's dependency is then already loaded.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable
from pathlib import Path

#: Load order matters: fontconfig links freetype.
LIBS = ("libfreetype.so.6", "libfontconfig.so.1")

#: Where distributions keep their 64-bit libraries (Debian, Fedora/openSUSE, Arch).
HOST_DIRS = (
    "/usr/lib/x86_64-linux-gnu",
    "/lib/x86_64-linux-gnu",
    "/usr/lib64",
    "/lib64",
    "/usr/lib",
)

FALLBACK_DIR = "fallback-libs"


def _is_elf64(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(5)
    except OSError:
        return False
    return head[:4] == b"\x7fELF" and head[4:5] == b"\x02"


def _host_copy(name: str, host_dirs: Iterable[str | Path]) -> Path | None:
    for folder in host_dirs:
        candidate = Path(folder) / name
        if candidate.is_file() and _is_elf64(candidate):
            return candidate
    return None


def _load_global(path: str) -> None:
    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)


def prefer_host_font_libs(
    fallback: str | Path,
    *,
    host_dirs: Iterable[str | Path] = HOST_DIRS,
    load: Callable[[str], object] = _load_global,
) -> dict[str, str]:
    """Load each font library once: the host's copy, else the bundled one. Never raises.

    Returns ``{name: "host" | "bundled" | "none"}``; empty outside the AppImage (no
    ``fallback`` folder), where nothing is loaded.
    """
    fallback = Path(fallback)
    if not fallback.is_dir():
        return {}
    host_dirs = list(host_dirs)
    used: dict[str, str] = {}
    for name in LIBS:
        used[name] = "none"
        host = _host_copy(name, host_dirs)
        if host is not None:
            try:
                load(str(host))
                used[name] = "host"
                continue
            except OSError:
                pass  # too new for this system's libc, broken, ...: use ours
        bundled = fallback / name
        if bundled.is_file():
            try:
                load(str(bundled))
                used[name] = "bundled"
            except OSError:
                pass
    return used


def prefer_host_font_libs_frozen() -> dict[str, str]:
    """:func:`prefer_host_font_libs` for the running frozen app (next to the executable)."""
    import sys

    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return prefer_host_font_libs(Path(base) / FALLBACK_DIR)


__all__ = [
    "FALLBACK_DIR",
    "HOST_DIRS",
    "LIBS",
    "prefer_host_font_libs",
    "prefer_host_font_libs_frozen",
]
