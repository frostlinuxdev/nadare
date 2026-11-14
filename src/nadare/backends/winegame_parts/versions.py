# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The install marker and the recorded installed and published versions.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...core import (
    paths,
)
from ...core.backends import (
    BackendError,
)
from ...core.gameversion import (
    VERSION_FILE,
    read_version_record,
)
from .shared import (
    _atomic_write_json,
)

log = logging.getLogger("nadare.backends.winegame")

#: Present in ``install_dir`` while a direct sync runs and left behind when it is
#: interrupted. Status and verify treat it as "incomplete", so a cancelled or
#: failed install/update is not mistaken for a finished one just because the main
#: executable is already on disk. Installs from before this existed have no
#: marker and still count as installed.
INSTALL_MARKER = ".nadare-install-incomplete"

#: The marker's second line when the sync it guards updates a finished install (#831).
_UPDATE_LINE = re.compile(r"^update from (\S+) to (\S+)$")


def _marker_version(version: str) -> str:
    """A version as the marker can hold it: no whitespace, at most 64 characters."""
    return re.sub(r"\s", "_", str(version))[:64]

#: Written after a successful direct sync: the manifest version actually installed.
#: (``VERSION_FILE`` is owned by :mod:`nadare.core.gameversion`.)

#: How long a fetched published version stays fresh (6 hours).
UPDATE_CHECK_INTERVAL = 6 * 60 * 60


class VersionsMixin:
    """Mixin for :class:`WineGameBackend`; see the module text."""

    def _install_marker_path(self) -> Path:
        return self.install_dir() / INSTALL_MARKER

    def _write_install_marker(self, update: tuple[str, str] | None = None) -> None:
        """Write the install-incomplete marker without following a link planted at its path.

        ``update`` (old, new version) records that the sync updates a finished install, so a
        restart after an interruption can say "update interrupted" instead of "not installed".

        The marker sits in a folder a game can write to. A plain ``write_text`` would truncate
        whatever a symlink there points at; a fresh sibling file moved over the name replaces the
        link itself (#964).
        """
        marker = self._install_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=marker.name + ".", dir=marker.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("sync in progress\n")
                if update is not None:
                    old, new = (_marker_version(part) for part in update)
                    handle.write(f"update from {old} to {new}\n")
            os.replace(name, marker)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def _install_incomplete(self) -> bool:
        return self._install_marker_path().is_file()

    def _interrupted_update(self) -> tuple[str, str] | None:
        """``(old, new)`` when the incomplete sync was an update of a finished install (#831).

        The marker sits where the game can write, so a link there is not followed and only a
        little of the file is read.
        """
        try:
            fd = os.open(self._install_marker_path(), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(1024)
        except OSError:
            return None
        for line in raw.decode("utf-8", "replace").splitlines():
            match = _UPDATE_LINE.match(line.strip())
            if match:
                return _marker_version(match.group(1)), _marker_version(match.group(2))
        return None

    def _version_path(self) -> Path:
        return self.install_dir() / VERSION_FILE

    def _read_version_file(self) -> dict[str, Any] | None:
        """The version record from the last successful sync, or ``None``.

        Missing, unreadable or malformed files are treated as "not recorded"
        (installs from before this existed have no file).
        """
        payload = read_version_record(self.install_dir())
        if payload is None:
            return None
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            return None
        return payload

    def _write_version_file(self, version: str, game_tag: str) -> None:
        _atomic_write_json(
            self._version_path(),
            {
                "version": str(version),
                "game_tag": str(game_tag),
                "synced_at": datetime.now(UTC).isoformat(),
            },
        )

    def _latest_variant(self) -> str:
        """What besides the game decides the version published to this profile (default: nothing).

        A game whose download differs by an option (Wuthering Waves' resource quality) gives it
        here, so each variant caches its own latest version (#975)."""
        return ""

    def _latest_path(self) -> Path:
        variant = self._latest_variant()
        name = f"{self.game_tag}-{variant}" if variant else self.game_tag
        return paths.cache_dir() / "yostar-latest" / f"{name}.json"

    def _read_latest(self) -> dict[str, Any] | None:
        """The cached published version, or ``None`` when absent/corrupt."""
        try:
            payload = json.loads(self._latest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            return None
        return payload

    def _write_latest(self, version: str) -> None:
        _atomic_write_json(
            self._latest_path(),
            {"version": str(version), "checked_at": int(time.time())},
        )

    def _record_synced_version(self, manifest: Any) -> None:
        """Remember what a finished sync installed and publish it as latest."""
        try:
            self._write_version_file(manifest.version, manifest.game_tag)
            self._write_latest(manifest.version)
        except OSError as exc:
            # A read-only install tree must not turn a finished update into a failure.
            log.debug("%s: could not record the synced version: %s", self.display_name, exc)

    def check_update(self) -> None:
        """Refresh the cached published version (direct mode only, offline-safe)."""
        if not self.direct_mode():
            return
        self._refresh_published_version()

    def refresh_published_version(self) -> None:
        """Refresh the cached published version when it is stale (direct mode; never raises)."""
        if self.direct_mode():
            self._refresh_published_version()

    def _refresh_published_version(self) -> None:
        """Refresh a stale cached version, swallowing manifest and cache errors."""
        try:
            if not self.ctx.config.data.get("check_updates", True):
                return
        except AttributeError:
            pass
        cached = self._read_latest()
        if cached is not None:
            try:
                age = time.time() - float(cached.get("checked_at", 0))
            except (TypeError, ValueError):
                age = UPDATE_CHECK_INTERVAL
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
            manifest = self._load_manifest()
        except (BackendError, RuntimeError, OSError) as exc:
            log.debug("%s: update check failed: %s", self.display_name, exc)
            return
        try:
            self._write_latest(manifest.version)
        except OSError as exc:
            log.debug("%s: could not cache the published version: %s", self.display_name, exc)
