# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Profile-based configuration with atomic saves and safe migration.

A profile is a dict. Shared keys live at the top level; per-game keys live under
``profiles``. ``active_profile`` selects one. Unknown keys are preserved so a
newer Nadare can add fields without clobbering them.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

#: Longest accepted profile name; also keeps derived log/path names sane.
MAX_PROFILE_NAME = 64

#: Characters that can never appear in a profile name: POSIX and Windows path
#: separators. Everything else printable (including Unicode) is allowed.
_PROFILE_NAME_SEPARATORS = frozenset("/\\")


def valid_profile_name(name: Any) -> bool:
    """True when ``name`` is safe as a profile key and filesystem segment.

    Profile names are joined onto :func:`nadare.core.paths.profiles_dir` (and
    used in log filenames), so a hand-edited or imported config must not smuggle
    a traversal, an absolute path or a drive letter in. Unicode letters and
    punctuation are allowed so existing non-ASCII profile names stay usable;
    empty names, ``.``/``..``, path separators, control/format characters,
    absolute paths and drive letters are always rejected, with a length cap.
    """
    if not isinstance(name, str) or not name:
        return False
    if name != name.strip() or len(name) > MAX_PROFILE_NAME:
        return False
    if name in (".", "..") or _PROFILE_NAME_SEPARATORS.intersection(name):
        return False
    if Path(name).is_absolute() or re.match(r"^[A-Za-z]:", name):
        return False
    # ``isprintable`` rejects control/format characters and line/paragraph
    # separators while allowing Unicode letters and punctuation.
    return name.isprintable()


def _sanitize_profile_name(name: str) -> str:
    """Best-effort safe key for a hand-edited/imported profile name.

    Path separators and non-printable characters become ``-``, a leading drive
    letter is stripped, surrounding whitespace is trimmed and the result is
    truncated to :data:`MAX_PROFILE_NAME`.  The output is always either a valid
    profile name or ``""`` (the caller then falls back to ``"profile"``), so a
    migrated key can never fail :func:`valid_profile_name`.
    """
    cleaned = "".join(
        "-" if (ch in _PROFILE_NAME_SEPARATORS or not ch.isprintable()) else ch
        for ch in name
    )
    cleaned = cleaned.strip()
    # ``C:evil`` survives the separator pass (``:`` is printable) but still
    # fails validation via the drive-letter rule.
    cleaned = re.sub(r"^[A-Za-z]:", "", cleaned).strip()
    if cleaned in (".", "..") or not cleaned:
        return ""
    cleaned = cleaned[:MAX_PROFILE_NAME].strip()
    if not valid_profile_name(cleaned):
        return ""
    return cleaned


def _unique_profile_key(base: str, taken: set[str]) -> str:
    """Return ``base`` or a ``base-<n>`` variant not already in ``taken``."""
    if base not in taken:
        return base
    index = 2
    while True:
        suffix = f"-{index}"
        candidate = f"{base[: MAX_PROFILE_NAME - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        index += 1


#: Defaults applied to every profile that lacks the key.
PROFILE_DEFAULTS: dict[str, Any] = {
    "backend": "",  # registry key, e.g. "stove-czn"
    "title": "",
    "install_dir": "",
    "prefix": "",
    "runner": "",  # Proton/GE/DW runner path or tag
    "launch_mode": "",  # backend-specific hint
    "remember_me": False,  # CZN: persist the launcher refresh token in the keyring
    "custom_background": "",
    "extra": {},
}

TOP_DEFAULTS: dict[str, Any] = {
    "schema": 1,
    "active_profile": "",
    "lock_to_single_instance": True,
    "check_updates": True,
    "language": "en",
    "telemetry": False,  # always false; kept explicit for the record
}


class ConfigReadError(OSError):
    """The config file exists but cannot be read; it is left untouched."""


@dataclass
class Config:
    path: Path = field(default_factory=paths.config_file)
    data: dict[str, Any] = field(default_factory=dict)
    #: Serialises mutation + :meth:`save` so a worker thread cannot race the
    #: GUI thread's write (and ``json.dump`` never sees a dict change size).
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    #: One-line user-facing note from :meth:`load` (a quarantined config).
    notice: str | None = field(default=None, repr=False, compare=False)
    #: Profiles deleted in this process, so :meth:`save` does not resurrect
    #: them when merging with the on-disk file.
    _removed: set[str] = field(default_factory=set, repr=False, compare=False)
    #: Profiles as last loaded/saved; a profile still equal to it was not
    #: touched here, so the on-disk copy wins on save.
    _snapshot: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---- lifecycle ---------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        p = Path(path) if path else paths.config_file()
        notice = None
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = None
            except OSError as exc:
                raise ConfigReadError(f"cannot read {p}: {exc.strerror or exc}") from exc
            try:
                if text is None:
                    raise ValueError("config is not valid UTF-8")
                raw = json.loads(text)
                if not isinstance(raw, dict):
                    raise ValueError("config root is not an object")
            except Exception:
                backup = cls._quarantine(p)
                raw = {}
                notice = (
                    f"{p} was unreadable (not valid JSON); "
                    + (f"it was moved to {backup}" if backup else "it could not be backed up")
                    + " and a fresh config is in use"
                )
        else:
            raw = {}
        cfg = cls(path=p, data=cls._normalise(raw), notice=notice)
        cfg._snapshot = copy.deepcopy(cfg.data["profiles"])
        return cfg

    @staticmethod
    def _quarantine(p: Path) -> Path | None:
        """Move a malformed config aside so a later ``save()`` cannot destroy it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # A second-granularity stamp collides when two corrupt configs land in
        # the same second; a short uuid keeps every quarantine distinct.
        suffix = uuid.uuid4().hex[:8]
        try:
            target = p.with_name(f"{p.name}.corrupt-{stamp}-{suffix}")
            os.replace(p, target)
            return target
        except OSError:
            return None

    @staticmethod
    def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(TOP_DEFAULTS)
        data.update(raw)
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        # Keys that are already safe are reserved up front so a migrated name
        # can never collide with one of them.
        reserved = {str(name) for name in profiles if valid_profile_name(str(name))}
        norm: dict[str, dict[str, Any]] = {}
        taken: set[str] = set()
        renames: dict[str, str] = {}
        for name, prof in profiles.items():
            key = str(name)
            if valid_profile_name(key):
                final = key
            else:
                # Never silently drop a profile: migrate an unsafe key to a safe
                # one, preserving the entry's install_dir/prefix/runner. Only
                # creation (``add_profile``/the GUI) rejects outright.
                base = _sanitize_profile_name(key) or "profile"
                final = _unique_profile_key(base, taken | reserved)
                renames[key] = final
                log.warning("migrated unsafe profile name %r to %r", key, final)
            taken.add(final)
            merged = dict(PROFILE_DEFAULTS)
            if isinstance(prof, dict):
                merged.update(prof)
            norm[final] = merged
        data["profiles"] = norm
        # Re-point the active profile through the rename map, so migrating the
        # active key does not silently select a different game.
        active = data.get("active_profile")
        if not isinstance(active, str):
            active = data["active_profile"] = ""
        if not isinstance(data.get("language"), (str, type(None))):
            data["language"] = TOP_DEFAULTS.get("language")
        if active in renames and renames[active] in norm:
            data["active_profile"] = renames[active]
        if data.get("active_profile") not in norm:
            data["active_profile"] = next(iter(norm), "")
        return data

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the config lock across a mutation followed by :meth:`save`.

        :meth:`BackendContext.set` runs on worker threads; taking this lock
        around the mutation and the write serialises it against a GUI-thread
        save and stops ``json.dump`` from iterating a dict that another thread
        is resizing.  The lock is re-entrant, so ``save()`` may be called from
        inside the block.
        """
        with self._lock:
            yield

    def save(self) -> Path:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._merge_disk_profiles()
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, sort_keys=True)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                _sync_dir(self.path.parent)
                self._snapshot = copy.deepcopy(self.profiles)
                self._removed.clear()
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return self.path

    def _merge_disk_profiles(self) -> None:
        """Keep profiles another process wrote since we loaded.

        Profiles only on disk are adopted unless this process removed them;
        profiles present here win. Called with the lock held, before writing.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        disk = raw.get("profiles") if isinstance(raw, dict) else None
        if not isinstance(disk, dict):
            return
        for name, prof in disk.items():
            if not isinstance(prof, dict) or not valid_profile_name(name):
                continue
            if name in self._removed:
                continue
            untouched = name in self.profiles and self._snapshot.get(name) == self.profiles[name]
            if name not in self.profiles or untouched:
                merged = dict(PROFILE_DEFAULTS)
                merged.update(prof)
                self.profiles[name] = merged
        if not self.data.get("active_profile") and self.profiles:
            self.data["active_profile"] = next(iter(self.profiles))

    # ---- profiles ----------------------------------------------------------
    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return self.data["profiles"]

    @property
    def active(self) -> str:
        return self.data.get("active_profile", "")

    def profile(self, name: str | None = None) -> dict[str, Any]:
        key = name or self.active
        if key not in self.profiles:
            raise KeyError(f"no such profile: {key!r}")
        return self.profiles[key]

    def add_profile(self, name: str, **values: Any) -> dict[str, Any]:
        if not valid_profile_name(name):
            raise ValueError(f"invalid profile name: {name!r}")
        prof = dict(PROFILE_DEFAULTS)
        prof.update(values)
        self.profiles[name] = prof
        self._removed.discard(name)
        if not self.active:
            self.data["active_profile"] = name
        return prof

    def remove_profile(self, name: str) -> None:
        """Delete ``name`` and re-point ``active_profile`` if it pointed there."""
        self.profiles.pop(name, None)
        self._removed.add(name)
        if self.data.get("active_profile") == name:
            self.data["active_profile"] = next(iter(self.profiles), "")

    def set_active(self, name: str) -> None:
        if name not in self.profiles:
            raise KeyError(name)
        self.data["active_profile"] = name


def _sync_dir(path: Path) -> None:
    """Best-effort ``fsync`` of a directory so a rename survives a power loss."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
