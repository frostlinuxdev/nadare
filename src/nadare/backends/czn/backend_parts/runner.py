# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The pinned runner: finding, downloading and naming it.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ....core import download as net
from ....core import runners as core_runners
from ....core.backends import (
    BackendError,
)
from ....core.events import Cancelled, Event

log = logging.getLogger("nadare.backends.czn.backend")

DEFAULT_RUNNER = core_runners.DEFAULT_FAMILY  # the tested DW-Proton release

#: Runner-install failure codes -> a user-safe message. 4 means the GitHub
#: runner download failed, not that the game's STOVE server is unreachable.
RUNNER_EXIT_MESSAGES: dict[int, str] = {
    1: "Nadare could not install the required Proton runner. Try Install again, or "
    "choose a runner in Settings → Manage runners.",
    4: "Could not download the required Proton runner from GitHub. Check your internet "
    "connection and try again, or choose a runner in Settings → Manage runners.",
    9: "The downloaded Proton runner was damaged or changed. Try Install again.",
}

#: Exit code the runner downloader uses for a checksum mismatch.
_RUNNER_INTEGRITY_EXIT = 9

_RUNNER_MATCH = {"cachyos": "cachyos", "ge": "GE-Proton", "dwproton": "dwproton"}


class RunnerMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    # ---- runner ------------------------------------------------------------
    def _installed_runner(self, rid: str) -> Path | None:
        root = self._runners_dir()
        if not root.is_dir():
            return None
        rid = rid.strip()
        exact = root / rid
        if rid and not rid.startswith(".") and (exact / "proton").is_file():
            return exact  # "GE-Proton9-1" must not resolve to GE-Proton9-10
        def matches(name: str) -> bool:
            if rid in core_runners.RUNNERS:
                return core_runners.runner_family(name) == rid
            # A tag names one release, not every name containing it.
            return core_runners.matches_tag(name, rid)

        cands = [
            d
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "proton").is_file()
            and matches(d.name)
        ]
        def _key(d: Path) -> list[int]:
            return [int(n) for n in re.findall(r"\d+", d.name)]

        return max(cands, key=lambda d: (_key(d), d.name)) if cands else None

    def _installed_pinned(self, rid: str) -> Path | None:
        """The already-installed *tested* release of ``rid``, if there is one."""
        tag = core_runners.pinned_tag(rid)
        root = self._runners_dir()
        if not tag or not root.is_dir():
            return None
        for d in sorted(root.iterdir()):
            if (
                d.is_dir()
                and not d.name.startswith(".")
                and (d / "proton").is_file()
                and core_runners.matches_tag(d.name, tag)
            ):
                return d
        return core_runners.installed_pinned(rid)

    def _pinned_runner(self, rid: str) -> Path:
        """The tested release of ``rid``: reuse it when installed, else download it once.

        A newer or older untested build that happens to be installed is not used
        in its place. Only when the pinned release cannot be downloaded (offline)
        does an installed build of the same family stand in, with a notice.
        """
        pinned = self._installed_pinned(rid)
        if pinned is not None:
            return pinned
        try:
            return self._download_runner(rid, latest=False)
        except BackendError as exc:
            if exc.code == _RUNNER_INTEGRITY_EXIT:
                raise  # a tampered or corrupt download must not fall back quietly
            fallback = self._installed_runner(rid)
            if fallback is None:
                raise
            self.ctx.emit(
                Event(
                    "status",
                    f"could not download the tested {rid} runner; using the installed "
                    f"{fallback.name} (untested)",
                )
            )
            return fallback

    def _download_runner(self, rid: str, latest: bool) -> Path:
        self.ctx.emit(Event("step", f"Downloading runner {rid}…"))
        try:
            return core_runners.install(
                rid, latest=latest, emit=self.ctx.emit, cancel=self.ctx.cancel
            )
        except Cancelled:
            raise
        except net.IntegrityError as exc:
            self._fail(f"runner {rid}", _RUNNER_INTEGRITY_EXIT, RUNNER_EXIT_MESSAGES, exc)
        except (net.DownloadError, OSError) as exc:
            self._fail(f"runner {rid}", 4, RUNNER_EXIT_MESSAGES, exc)
        except core_runners.RunnerError as exc:
            # "cannot query/fetch" is GitHub being unreachable; the rest is a
            # refusal (untrusted host, no tarball, no checksum to verify against).
            offline = str(exc).startswith(("cannot query", "cannot fetch"))
            self._fail(f"runner {rid}", 4 if offline else 1, RUNNER_EXIT_MESSAGES, exc)
        raise AssertionError("unreachable")  # pragma: no cover - _fail always raises

    def resolve_runner(self) -> tuple[str, str]:
        """Return ``(protonpath, kind)`` where kind is ``proton`` or ``wine``.

        ``profile['runner']`` may be a Proton directory, a runner id
        (``dwproton``/``cachyos``/``ge``) or ``GE-Proton``; empty falls back to
        the pinned tested release (DW-Proton).
        """
        rid = core_runners.profile_runner_value(self.ctx.profile)
        if not rid or rid.lower() in ("wine", "system", "none"):
            # One meaning in every Proton game: these words mean the default runner, never
            # plain system Wine (maintainer decision 2026-11-13, #1310 W9-1).
            rid = DEFAULT_RUNNER
        if rid in core_runners.RUNNERS:
            return str(self._pinned_runner(rid)), "proton"
        try:
            found = core_runners.resolve_runner(rid)
        except core_runners.RunnerError as exc:
            if rid.upper().startswith("GE-PROTON"):
                # Keep the old late failure for a tag that is not installed: launch
                # reports the missing umu/runner setup instead of "not found".
                return rid, "proton"
            raise BackendError(str(exc)) from exc
        if found is None:
            raise BackendError(f"unknown or missing runner: {rid!r}")
        return str(found), "proton"

    def runner_name(self) -> str:
        try:
            proton, kind = self.resolve_runner()
        except BackendError:
            return ""
        if kind == "wine":
            return "wine"
        return core_runners.runner_version(Path(proton))
