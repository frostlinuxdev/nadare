# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""XDG directories for Nadare. Every module uses these; nothing hard-codes ~."""

from __future__ import annotations

import os
from pathlib import Path

APP = "nadare"


def _xdg(env: str, default: str) -> Path:
    base = os.environ.get(env) or os.path.expanduser(default)
    return Path(base) / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", "~/.local/share")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", "~/.local/state")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", "~/.cache")


def config_file() -> Path:
    return config_dir() / "config.json"


def logs_dir() -> Path:
    return state_dir() / "logs"


def runners_dir() -> Path:
    return data_dir() / "runners"


def profiles_dir() -> Path:
    """Per-profile game trees / prefixes / stubs."""
    return data_dir() / "profiles"


def ensure_dirs() -> None:
    for d in (
        data_dir(),
        config_dir(),
        state_dir(),
        cache_dir(),
        logs_dir(),
        runners_dir(),
        profiles_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            # mkdir(mode=) only applies to a directory it creates: tighten dirs
            # an earlier version made with the umask.
            os.chmod(d, 0o700)
        except OSError:
            pass
