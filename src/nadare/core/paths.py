# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""XDG directories for Nadare, and the one engine that validates untrusted relative paths.

Every module uses these; nothing hard-codes ~. ``check_relative`` with a ``RelativePolicy`` is the
only place the rules for a manifest path (backslashes, ``..``, absolute, NUL) are written down.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import channel

#: The stable build's folder name; :func:`app_name` is the one every path uses.
APP = "nadare"


def app_name() -> str:
    """``nadare``, or ``nadare-unstable`` for an unstable build (its own folders)."""
    return APP + channel.suffix()


def _xdg(env: str, default: str) -> Path:
    base = os.environ.get(env, "")
    if not os.path.isabs(base):  # the XDG spec: a relative value is invalid and must be ignored
        base = os.path.expanduser(default)
    return Path(base) / app_name()


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


class PathProblem(ValueError):
    """A relative path was rejected; ``code`` names the rule that failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RelativePolicy:
    """How one manifest format's untrusted relative paths are validated.

    ``backslash``: ``"reject"`` refuses any backslash; ``"convert"`` turns it into ``/``
    before validation.
    ``strip``: trim surrounding whitespace before checking.
    ``reject_nul``: refuse NUL bytes.
    ``reject_drive``: refuse a leading drive letter such as ``C:``.
    ``dots``: how to treat ``.`` and ``..``: ``"drop"`` removes empty and ``.`` parts and
    refuses ``..``, ``"reject"`` refuses any empty, ``.`` or ``..`` component, and
    ``"normpath"`` normalises with ``posixpath`` and then refuses an escape.

    The three presets below reproduce what the direct-download, Sophon and Yostar
    validators each did before they shared this engine, including where they differ, so
    a fix to a shared rule lands once and a deliberate difference is visible here.
    """

    backslash: Literal["reject", "convert"]
    strip: bool
    reject_nul: bool
    reject_drive: bool
    dots: Literal["drop", "reject", "normpath"]


DIRECT_PATHS = RelativePolicy(
    backslash="reject",
    strip=False,
    reject_nul=True,  # a NUL from a plan path reached stat() as a raw ValueError
    reject_drive=False,
    dots="drop",
)

SOPHON_PATHS = RelativePolicy(
    backslash="reject",
    strip=False,
    reject_nul=True,  # a NUL escaped as a raw ValueError from stat() (#1207 b104)
    reject_drive=False,
    dots="reject",
)

MANIFEST_PATHS = RelativePolicy(
    backslash="convert",
    strip=True,
    reject_nul=True,
    reject_drive=True,
    dots="normpath",
)

SEVENZ_PATHS = RelativePolicy(
    backslash="convert",
    strip=False,
    reject_nul=True,
    reject_drive=True,
    dots="drop",
)

#: Nadare's own folders in an install (``.nadare``, ``.nadare-dl``, journal/state names). A
#: publisher path may never reach one: a forged ``.done`` record would make an install look done.
NADARE_OWNED_PREFIX = ".nadare"


def _reject_nadare_owned(relative: str) -> str:
    """Return ``relative``, or reject it when its first component belongs to Nadare."""
    if relative.split("/", 1)[0].casefold().startswith(NADARE_OWNED_PREFIX):
        raise PathProblem("nadare-owned")
    return relative


def check_relative(path: object, policy: RelativePolicy) -> str:
    """Return ``path`` as a normalised POSIX relative path, or raise :class:`PathProblem`."""
    if not isinstance(path, str):
        raise PathProblem("not-string")
    text = path
    if policy.backslash == "convert":
        text = text.replace("\\", "/")
    if policy.strip:
        text = text.strip()
    if not text:
        raise PathProblem("empty")
    if policy.backslash == "reject" and "\\" in text:
        raise PathProblem("backslash")
    if policy.reject_nul and "\x00" in text:
        raise PathProblem("nul")
    if text.startswith("/"):
        raise PathProblem("absolute")
    if policy.reject_drive and re.match(r"^[A-Za-z]:", text):
        raise PathProblem("absolute")
    if policy.dots == "drop":
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise PathProblem("dotdot")
        if not parts:
            raise PathProblem("empty-normalised")
        return _reject_nadare_owned("/".join(parts))
    if policy.dots == "reject":
        for part in text.split("/"):
            if part in ("", ".", ".."):
                raise PathProblem("component")
        return _reject_nadare_owned(text)
    norm = posixpath.normpath(text)
    if norm in (".", "..") or norm.startswith("../") or "/../" in norm:
        raise PathProblem("escape")
    if norm.startswith("/"):
        raise PathProblem("absolute")
    return _reject_nadare_owned(norm)
