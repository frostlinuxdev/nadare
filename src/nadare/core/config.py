# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Profile-based configuration with atomic saves and safe migration.

A profile is a dict. Shared keys live at the top level; per-game keys live under
``profiles``. ``active_profile`` selects one. Unknown keys are preserved so a
newer Nadare can add fields without clobbering them.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import shutil
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
    # A leading ``-`` would be parsed as an option (``nadare launch -p -demo``).
    if name.startswith("-"):
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


def _coerce_bool(value: Any, default: bool) -> bool:
    """Turn a hand-edited boolean into a real ``bool`` (or keep the default).

    ``json`` keeps ``"false"`` a string, which is truthy; accepting the common
    string/integer spellings prevents a setting from silently flipping on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
        if text in ("0", "1"):
            return text == "1"
    return default


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
    #: The same for the top-level settings, so a save only overwrites the keys
    #: this process actually changed.
    _top_snapshot: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: Unsafe profile keys renamed by :meth:`load`, so a save still finds the
    #: on-disk entry instead of dropping the migrated profile.
    _renames: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

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
                if cls._bad_profiles(raw):
                    # Valid JSON, wrong shape: normalising would drop entries and
                    # the next save would overwrite them. Keep a copy first.
                    backup = cls._quarantine(p, keep=True)
                    saved = f"saved to {backup}" if backup else "could not be backed up"
                    notice = f"{p} has profile entries that are not objects and were reset; {saved}"
                elif cls._missing_profiles_section(raw):
                    # A typo such as ``"profile"`` would otherwise look like an
                    # empty library, and the next save would erase the file.
                    backup = cls._quarantine(p, keep=True)
                    saved = (
                        f"a copy was saved to {backup}"
                        if backup
                        else "a copy could not be backed up"
                    )
                    notice = f"{p} has no profiles section; {saved}"
        else:
            raw = {}
        renames: dict[str, str] = {}
        cfg = cls(path=p, data=cls._normalise(raw, renames), notice=notice)
        cfg._renames = dict(renames)
        if renames:
            moved = cfg._migrate_profile_dirs(renames)
            if moved:
                detail = "; ".join(f"{old!r} to {new!r}" for old, new in moved)
                message = (
                    f"Nadare renamed the profile {detail} and moved its game folder to match, "
                    "so your game and settings are still there."
                )
                cfg.notice = f"{notice}\n\n{message}" if notice else message
        cfg._snapshot = copy.deepcopy(cfg.data["profiles"])
        cfg._top_snapshot = cfg._top_level()
        return cfg

    def _migrate_profile_dirs(self, renames: dict[str, str]) -> list[tuple[str, str]]:
        """Move ``profiles/<old>`` to ``profiles/<new>`` for renamed keys.

        Only the default tree is moved, and only when the old one exists and the
        new one does not, so a user's explicit paths or an existing new tree are
        never overwritten. Returns the renames that actually moved a folder.
        """
        root = paths.profiles_dir()
        moved: list[tuple[str, str]] = []
        for old, new in renames.items():
            relative = Path(old)
            # An unsafe key can contain ``..`` or be absolute; never follow it
            # outside the profiles root while cleaning up its old default tree.
            if relative.is_absolute() or ".." in relative.parts:
                continue
            old_dir = root / relative
            new_dir = root / new
            if not old_dir.is_dir() or new_dir.exists():
                continue
            try:
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                old_dir.rename(new_dir)
            except OSError as exc:
                log.warning("could not move profile folder %s to %s: %s", old_dir, new_dir, exc)
                continue
            self._repoint_paths(self.profiles.get(new, {}), old_dir, new_dir)
            moved.append((old, new))
        return moved

    @staticmethod
    def _repoint_paths(value: Any, old_dir: Path, new_dir: Path) -> None:
        """Rewrite stored paths under ``old_dir`` to ``new_dir`` (the folder was just moved)."""
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in list(items):
            if isinstance(item, str):
                try:
                    inside = Path(item).is_relative_to(old_dir)
                except ValueError:
                    inside = False
                if inside:
                    value[key] = str(new_dir / Path(item).relative_to(old_dir))
            elif isinstance(item, (dict, list)):
                Config._repoint_paths(item, old_dir, new_dir)

    @staticmethod
    def _bad_profiles(raw: dict[str, Any]) -> bool:
        """True when ``profiles`` (or an entry in it) has the wrong JSON type."""
        if "profiles" not in raw:
            return False
        profiles = raw["profiles"]
        if not isinstance(profiles, dict):
            return True
        return any(not isinstance(prof, dict) for prof in profiles.values())

    @staticmethod
    def _missing_profiles_section(raw: dict[str, Any]) -> bool:
        """True when an existing config looks like it lost its ``profiles`` key.

        Only a file that carries data beyond the known top-level settings is
        suspicious: an empty object or a fresh settings-only config is a
        legitimate starting point. A key whose value is a dict of dicts is the
        giveaway for a typo such as ``"profile"`` or ``"games"``.
        """
        if not raw or "profiles" in raw:
            return False
        for value in raw.values():
            if (
                isinstance(value, dict)
                and value
                and all(isinstance(item, dict) for item in value.values())
            ):
                return True
        return any(key not in TOP_DEFAULTS for key in raw)

    @staticmethod
    def _quarantine(p: Path, *, keep: bool = False) -> Path | None:
        """Move (or, with ``keep``, copy) a malformed config aside.

        Moving stops a later ``save()`` destroying the file; copying keeps a
        config that is still loadable in place.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # A second-granularity stamp collides when two corrupt configs land in
        # the same second; a short uuid keeps every quarantine distinct.
        suffix = uuid.uuid4().hex[:8]
        try:
            target = p.with_name(f"{p.name}.corrupt-{stamp}-{suffix}")
            if keep:
                shutil.copy2(p, target)
            else:
                os.replace(p, target)
            return target
        except OSError:
            return None

    @staticmethod
    def _normalise(
        raw: dict[str, Any], renames: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Merge defaults, migrate unsafe keys and normalise older profile aliases.

        ``renames`` is filled with every unsafe-key migration so :meth:`load`
        can move the matching default data folder before the user sees it.
        """
        data = dict(TOP_DEFAULTS)
        data.update(raw)
        # A hand-edited ``"false"`` is a truthy string; coerce known booleans so
        # the setting means what the file says.
        for key, default in TOP_DEFAULTS.items():
            if isinstance(default, bool):
                data[key] = _coerce_bool(data.get(key, default), default)
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        # Keys that are already safe are reserved up front so a migrated name
        # can never collide with one of them.
        reserved = {str(name) for name in profiles if valid_profile_name(str(name))}
        norm: dict[str, dict[str, Any]] = {}
        taken: set[str] = set()
        if renames is None:
            renames = {}
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
            Config._normalise_legacy_keys(merged)
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

    @staticmethod
    def _normalise_legacy_keys(profile: dict[str, Any]) -> None:
        """Move old beta aliases onto the canonical keys, without losing values.

        When a canonical key is already set it wins; a conflicting old value is
        kept under ``extra["legacy"]`` so no hand-written data disappears.
        """
        extra = profile.get("extra")
        if not isinstance(extra, dict):
            extra = None
        for old, new in (("proton_runner", "runner"), ("wineprefix", "prefix")):
            legacy = profile.get(old)
            if legacy in (None, "") and extra is not None:
                legacy = extra.get(old)
            current = profile.get(new)
            if current in (None, "") and extra is not None:
                current = extra.get(new)
            if current in (None, "") and legacy not in (None, ""):
                profile[new] = legacy
                log.info("migrated old profile key %r to %r", old, new)
            elif legacy not in (None, "") and legacy != current:
                if extra is None:
                    extra = profile["extra"] = {}
                kept = extra.get("legacy")
                if not isinstance(kept, dict):
                    kept = extra["legacy"] = {}
                kept[old] = legacy
                log.info("kept old profile key %r under extra['legacy']", old)
            profile.pop(old, None)
            if extra is not None:
                extra.pop(old, None)

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

    def _top_level(self) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in self.data.items() if k != "profiles"}

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Cross-process lock around the read-merge-write in :meth:`save`."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self) -> Path:
        # Replace the file a symlink points at, not the link itself: a config
        # kept in a dotfiles repo must stay linked after a save.
        target = Path(os.path.realpath(self.path))
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock():
                self._merge_disk_profiles()
                fd, tmp = tempfile.mkstemp(
                    dir=str(target.parent), prefix=".config-", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(self.data, f, indent=2, sort_keys=True)
                        f.write("\n")
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, target)
                    _sync_dir(target.parent)
                    self._snapshot = copy.deepcopy(self.profiles)
                    self._top_snapshot = self._top_level()
                    self._removed.clear()
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            return self.path

    def disk_profile_names(self) -> set[str] | None:
        """Profile names currently in the file, or ``None`` when it cannot be read.

        ``None`` (unreadable, missing, not JSON, no ``profiles`` object) must never
        be mistaken for "every profile was removed".
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        profiles = raw.get("profiles")
        if not isinstance(profiles, dict):
            return None
        return {
            self._renames.get(str(name), str(name))
            for name, prof in profiles.items()
            if isinstance(prof, dict)
        }

    @staticmethod
    def _merge_profile(
        snapshot: dict[str, Any], local: dict[str, Any], disk: dict[str, Any]
    ) -> dict[str, Any]:
        """Three-way merge mappings: local edits win, untouched keys take disk.

        Nested mappings are merged by key too, so independent edits (such as
        separate ``extra_env`` variables) are retained. A key removed locally
        stays removed even if the other process changed it; a disk-only key is
        kept.
        """
        missing = object()

        def merge_mapping(
            base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
        ) -> dict[str, Any]:
            merged = dict(ours)
            for key in set(base) | set(ours) | set(theirs):
                base_value = base.get(key, missing)
                our_value = ours.get(key, missing)
                if (
                    isinstance(base_value, dict)
                    and isinstance(our_value, dict)
                    and isinstance(theirs.get(key), dict)
                ):
                    merged[key] = merge_mapping(base_value, our_value, theirs[key])
                    continue
                if base_value != our_value:
                    continue  # changed locally: keep the local value (or its absence)
                if key in theirs:
                    merged[key] = theirs[key]
                else:
                    merged.pop(key, None)
            return merged

        merged = merge_mapping(snapshot, local, disk)
        Config._normalise_legacy_keys(merged)
        return merged

    def _merge_disk_profiles(self) -> None:
        """Fold in what another process wrote since we loaded.

        Profiles only on disk are adopted unless this process removed them; a
        profile this process left untouched but the disk no longer has was
        removed elsewhere and is dropped; profiles changed here win. Top-level
        settings this process did not change take the on-disk value. Called with
        the lock held, before writing.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        disk = raw.get("profiles")
        if not isinstance(disk, dict):
            return
        # Follow the same unsafe-key renames this process applied on load, so
        # the old on-disk name is not mistaken for a profile we removed.
        disk_profiles: dict[str, dict[str, Any]] = {}
        for name, prof in disk.items():
            if not isinstance(prof, dict):
                continue
            final = self._renames.get(str(name), str(name))
            if valid_profile_name(final):
                disk_profiles[final] = prof
        disk = disk_profiles
        for name in list(self.profiles):
            untouched = name in self._snapshot and self._snapshot[name] == self.profiles[name]
            if name not in disk and untouched:
                del self.profiles[name]
        for name, prof in disk.items():
            if not isinstance(prof, dict) or not valid_profile_name(name):
                continue
            if name in self._removed:
                continue
            if name not in self.profiles:
                # Only on disk: adopt it as before.
                merged = dict(PROFILE_DEFAULTS)
                merged.update(prof)
                Config._normalise_legacy_keys(merged)
                self.profiles[name] = merged
            elif name in self._snapshot:
                # Present here, in the last snapshot and on disk: merge per key.
                # Update in place: workers may hold a reference to this dict.
                local = self.profiles[name]
                merged = self._merge_profile(self._snapshot[name], local, prof)
                local.clear()
                local.update(merged)
            # A profile added locally but not saved yet keeps the local values.
        for key, value in raw.items():
            if key == "profiles" or key not in self._top_snapshot:
                continue
            if self.data.get(key) == self._top_snapshot[key]:
                self.data[key] = value
        active = self.data.get("active_profile")
        if not isinstance(active, str) or active not in self.profiles:
            self.data["active_profile"] = next(iter(self.profiles), "")

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
