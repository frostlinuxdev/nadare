# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generic native-Steam backend.

Ports the ``launch_mode="steam"`` row of the stella launcher: the client owns
its own compatibility prefix and downloads the game, so Nadare only hands it a
``steam://rungameid/<app_id>`` URI.  Steam exposes no per-game stop verb, so a
stop is a best-effort signal to matching processes (optionally shutting the
whole client down when the profile asks for it).

Profile keys (top level or under ``extra``):

``steam_app_id`` (or ``app_id``)
    the numeric store id, required.
``steam_binary``
    command, default ``steam``; may carry args (e.g. a Flatpak client).
``process_match``
    substring used to detect/stop the running game.
``stop_steam``
    also run ``steam -shutdown`` on stop.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from ..core.backends import BackendError, GameBackend, Option, Status
from ..core.events import Event, mask
from .wine import (
    ProcessQuery,
    cmdline_argv,
    profile_setting,
    remember_start,
    scan_pids,
    stop_pids,
    stream,
)

#: Steam's ``steamapps`` folders, native then Flatpak.
_STEAMAPPS_ROOTS = (
    Path(".local") / "share" / "Steam" / "steamapps",
    Path(".steam") / "steam" / "steamapps",
    Path(".var") / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "steamapps",
)

_LIBRARY_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')


def _steamapps_dirs() -> list[Path]:
    """Every plausible ``steamapps`` directory, including extra library folders."""
    home = Path(os.path.expanduser("~"))
    roots = [home / relative for relative in _STEAMAPPS_ROOTS]
    for root in list(roots):
        vdf = root / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _LIBRARY_PATH_RE.finditer(text):
            library = Path(match.group(1).replace("\\\\", "\\")).expanduser()
            if library.is_absolute():
                roots.append(library / "steamapps")
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen:
            continue
        seen.add(key)
        found.append(root)
    return found


def _has_app_manifest(app_id: str) -> bool:
    """True when Steam has an ``appmanifest_<id>.acf`` in one of its libraries."""
    name = f"appmanifest_{app_id}.acf"
    return any((root / name).is_file() for root in _steamapps_dirs())


def _applaunch_pids(app_id: str, *, proc_root: str | os.PathLike[str] = "/proc") -> list[int]:
    """PIDs carrying the exact ``-applaunch <app_id>`` argument pair.

    Steam re-execs itself as ``steam -applaunch <id>`` for a manual launch. The
    id is only unambiguous as a *pair* with ``-applaunch``: an exact-element
    match on the bare id would also catch an unrelated numeric argument.
    """
    me = os.getpid()
    uid = os.getuid()
    hits: list[int] = []
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return hits
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue  # never signal a Steam running as another user
        except OSError:
            continue
        argv = cmdline_argv(entry)
        if argv and any(
            token == "-applaunch" and argv[i + 1] == app_id for i, token in enumerate(argv[:-1])
        ):
            hits.append(pid)
    return remember_start(hits)


class SteamBackend(GameBackend):
    kind = "steam"
    supports_update = False  # Steam updates its own games

    options = (
        Option("steam_app_id", "Steam app id", "text", "Numeric store id, e.g. 4813850."),
        Option(
            "steam_binary",
            "Steam command",
            "text",
            "Steam client command (may include Flatpak arguments).",
            advanced=True,
        ),
        Option(
            "process_match",
            "Process match",
            "text",
            "Substring used to detect/stop the running game.",
            advanced=True,
        ),
        Option(
            "stop_steam",
            "Stop Steam on exit",
            "bool",
            "Run steam -shutdown when stopping.",
            advanced=True,
        ),
    )

    # ---- profile resolution -----------------------------------------------
    def _app_id(self) -> str:
        app_id = str(
            profile_setting(self.ctx.profile, "steam_app_id")
            or profile_setting(self.ctx.profile, "app_id")
            or ""
        ).strip()
        if not app_id:
            raise BackendError("steam profile has no steam_app_id")
        if not app_id.isdigit():
            raise BackendError(f"steam_app_id must be a numeric store id, got {app_id!r}")
        return app_id

    def _client(self) -> list[str]:
        configured = (
            str(profile_setting(self.ctx.profile, "steam_binary", "steam") or "steam").strip()
            or "steam"
        )
        try:
            parts = shlex.split(configured)
        except ValueError as exc:
            raise BackendError(f"invalid steam_binary {configured!r}: {exc}") from exc
        if not parts:
            parts = ["steam"]
        resolved = shutil.which(parts[0])
        if not resolved:
            raise BackendError(f"{parts[0]!r} is not on PATH (install Steam or set steam_binary)")
        return [resolved, *parts[1:]]

    def build(self) -> tuple[list[str], str]:
        app_id = self._app_id()
        return [*self._client(), f"steam://rungameid/{app_id}"], app_id

    def _patterns(self, app_id: str | None = None) -> ProcessQuery:
        """Exact argv tokens identifying a launch of ``app_id``.

        Nadare hands Steam ``steam://rungameid/<id>``, but Steam then
        re-launches the game as ``reaper SteamLaunch AppId=<id> …``, so both the
        launch URI and the ``AppId=`` token are matched as exact argv elements.
        The ``-applaunch <id>`` form is handled by :func:`_applaunch_pids`; the
        bare numeric id is never matched on its own.
        """
        extra = profile_setting(self.ctx.profile, "process_match")
        args = (f"steam://rungameid/{app_id}", f"AppId={app_id}") if app_id else ()
        return ProcessQuery(match=str(extra) if extra else "", args=args)

    def _matching_pids(
        self, app_id: str | None, *, proc_root: str | os.PathLike[str] = "/proc"
    ) -> list[int]:
        """PIDs of a running launch of ``app_id`` (URI, ``AppId=`` or ``-applaunch``)."""
        pids = set(scan_pids(self._patterns(app_id), proc_root=proc_root))
        if app_id:
            pids.update(_applaunch_pids(app_id, proc_root=proc_root))
        return sorted(pids)

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        data: dict[str, object] = {"can_install": False, "can_update": False}
        try:
            app_id = self._app_id()
        except BackendError as exc:
            return Status(state="error", message=str(exc), data=data)
        try:
            client = self._client()
        except BackendError:
            return Status(state="not_installed", message="Steam client not found", data=data)
        pids = self._matching_pids(app_id)
        installed = _has_app_manifest(app_id)
        data.update({"app_id": app_id, "client": client[0], "pids": pids, "installed": installed})
        if pids:
            return Status(state="running", running=True, data=data)
        if installed:
            return Status(state="installed", data=data)
        # Stay launchable: Steam itself opens the store/install page, and a library
        # in an unusual place must not turn the game into a dead end.
        return Status(
            state="installed",
            message="Steam will install or open this game (not found in your Steam library)",
            data=data,
        )

    def install(self) -> None:
        raise BackendError(
            "the steam backend does not install games; install them through the Steam client"
        )

    def update(self) -> None:
        raise BackendError("the steam backend does not update games; Steam does")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        argv, app_id = self.build()
        self.ctx.emit(Event("step", f"Steam: launching app {app_id}"))
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        return stream(
            argv,
            dict(os.environ),
            on_line=lambda line: self.info(mask(line)),
            cancel=self.ctx.cancel,
            stop_wine=False,
        )

    def stop(self) -> None:
        try:
            app_id: str | None = self._app_id()
        except BackendError:
            app_id = None
        pids = self._matching_pids(app_id)
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            stop_pids(pids)
        else:
            self.info("no matching Steam game process found")
        if profile_setting(self.ctx.profile, "stop_steam"):
            try:
                client = self._client()
                subprocess.run([*client, "-shutdown"], capture_output=True, timeout=20, check=False)
            except (BackendError, OSError, subprocess.TimeoutExpired):
                pass

    def verify(self) -> dict:
        try:
            app_id = self._app_id()
        except BackendError as exc:
            return {"verified": False, "message": str(exc)}
        try:
            self._client()
        except BackendError as exc:
            return {"verified": False, "message": str(exc)}
        return {"verified": True, "app_id": app_id}
