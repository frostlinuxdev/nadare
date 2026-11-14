# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""One install-folder lock shared by every direct sync implementation."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import paths, process

log = logging.getLogger(__name__)


@contextmanager
def hold(
    install_dir: str | os.PathLike[str],
    error: type[Exception],
    message: str,
) -> Iterator[None]:
    """Refuse a second sync into ``install_dir`` while the block runs.

    Direct downloads and Yostar manifests write the same ``.part`` files, so they
    must agree on the lock file. ``error`` is the caller's user-facing exception.
    """
    resolved = Path(install_dir).expanduser().resolve()
    key = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    lock_path = paths.state_dir() / "locks" / f"directdl-{key}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # An unusable lock folder is not "busy": name it (#1307 I2).
        raise error(
            f"cannot use the lock folder {lock_path.parent}: {exc.strerror or exc}"
        ) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if process.flock_unavailable(exc):
                # A filesystem without flock must not block every sync (#1307 I4).
                log.warning("file locking is unsupported on %s: %s", lock_path, exc)
            else:
                raise error(message) from None
        yield
    finally:
        os.close(fd)


__all__ = ["hold"]
