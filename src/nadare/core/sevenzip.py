# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate system or integrity-pinned bundled 7-Zip."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import paths, process

BUNDLED = Path(__file__).parents[1] / "assets" / "7zzs"
_SYSTEM_NAMES = ("7z", "7zz", "7za")
BUNDLED_SHA256 = "8952c73646dc8d4abfb31e48a949ba0ac15cb2437da9e4430886ddb6e12b383b"


def _digest(path: Path) -> str | None:
    try:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                value.update(block)
        return value.hexdigest()
    except OSError:
        return None


def describe(
    *, which: Callable[[str], str | None] | None = None, bundled: Path | None = None
) -> tuple[str, str | None]:
    """Where :func:`find` would get 7-Zip, without running or copying anything.

    Returns ``("system", path)``, ``("bundled", path)`` (digest-verified) or ``("missing", None)``.
    """
    lookup = shutil.which if which is None else which
    for name in _SYSTEM_NAMES:
        candidate = lookup(name)
        if candidate:
            return "system", candidate
    source = Path(BUNDLED if bundled is None else bundled)
    if _digest(source) == BUNDLED_SHA256:
        return "bundled", str(source)
    return "missing", None


def find(
    *,
    which: Callable[[str], str | None] = shutil.which,
    bundled: Path | None = None,
    expected_sha256: str = BUNDLED_SHA256,
    cache_dir: Callable[[], Path] = paths.cache_dir,
    run: Callable[..., object] = subprocess.run,
) -> str | None:
    """Return a system 7-Zip, or a verified bundled executable if available.

    ``bundled`` defaults to :data:`BUNDLED`, read at call time so tests can redirect it.
    """
    for name in _SYSTEM_NAMES:
        candidate = which(name)
        if candidate:
            return candidate

    source = Path(BUNDLED if bundled is None else bundled)
    if _digest(source) != expected_sha256:
        return None
    try:
        if source.stat().st_mode & 0o111:
            run(
                [str(source), "-h"],
                env=process.child_environ(),
                capture_output=True,
                check=False,
                timeout=5,
            )
            return str(source)
    except (OSError, subprocess.SubprocessError):
        pass

    destination = Path(cache_dir()) / "bin" / f"7zzs-{expected_sha256[:12]}"
    if _digest(destination) == expected_sha256:
        return str(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755)
        if _digest(temporary) != expected_sha256:
            return None
        os.replace(temporary, destination)
        if _digest(destination) != expected_sha256:
            destination.unlink(missing_ok=True)
            return None
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)
