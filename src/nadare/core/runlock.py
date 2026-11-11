# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A run lock so one Flatpak instance can see a game started by another.

Every ``flatpak run`` has its own process namespace, so ``/proc`` only shows the
processes of the instance that started them. The instance that launches a game
holds a lock file for the whole launch; another instance sees the lock and knows
the game is running, even though it cannot find the game's processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from . import paths


def _lock_path(prefix: str | os.PathLike[str]) -> Path:
    """The lock file for ``prefix``, named by its resolved path.

    Resolving means ``a/../prefix`` and ``prefix`` are the same lock; the hash
    keeps long or odd paths out of the file name.
    """
    resolved = Path(prefix).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]
    return paths.state_dir() / "running" / f"{digest}.lock"


def _stop_path(prefix: str | os.PathLike[str]) -> Path:
    return _lock_path(prefix).with_suffix(".stop")


@contextlib.contextmanager
def hold(prefix: str | os.PathLike[str]) -> Iterator[None]:
    """Hold the run lock for ``prefix`` while the block runs."""
    path = _lock_path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        # A stop request from before this run (its holder was killed before clearing it)
        # must not stop the game that starts now.
        try:
            _stop_path(prefix).unlink()
        except OSError:
            pass
        try:
            yield
        finally:
            # Clear this run's stop request while the lock is still held: one written after the
            # release is for whoever holds the lock next (final round).
            try:
                _stop_path(prefix).unlink()
            except OSError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def running(prefix: str | os.PathLike[str]) -> bool:
    """True when another open file holds the run lock for ``prefix``."""
    try:
        with _lock_path(prefix).open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def request_stop(prefix: str | os.PathLike[str]) -> None:
    """Ask the instance holding the run lock to stop its game."""
    path = _stop_path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def stop_requested(prefix: str | os.PathLike[str]) -> bool:
    return _stop_path(prefix).exists()


__all__ = ["hold", "request_stop", "running", "stop_requested"]


@contextlib.contextmanager
def guard(prefix: str | os.PathLike[str], on_stop: Callable[[], object]) -> Iterator[None]:
    """Hold the lock for a launch and call ``on_stop`` when another instance asks to stop it."""
    with hold(prefix):
        done = threading.Event()

        def watch() -> None:
            while not done.wait(0.2):
                if stop_requested(prefix):
                    on_stop()
                    return

        thread = threading.Thread(target=watch, daemon=True, name="nadare-runlock")
        thread.start()
        try:
            yield
        finally:
            done.set()
            # A stop that is running sweeps processes for up to 10 s: release the lock only after
            # it, or another window starts the game beside the one being stopped (final round).
            thread.join(timeout=30.0)
