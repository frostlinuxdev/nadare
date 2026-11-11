# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generic shell/script backend (``launch_mode="script"``).

A ``.sh`` runs on the host with bash; a Windows ``.bat``/``.cmd`` is not a host
script and is routed through the profile's Wine/Proton exactly like a
``wine-exe`` launch, so a batch installer lands in the prefix the profile
declares (this reuses :class:`~nadare.backends.wine.WineBackend`).

Profile keys (top level or under ``extra``):

``launch_exe`` (or ``script``/``exe_name``)
    the script to run; a bare name is resolved against ``install_dir``.
``process_match``
    substring used to detect/stop the running script.
"""

from __future__ import annotations

import os
import shlex
import signal
from pathlib import Path
from typing import Any

from ..core import process
from ..core.backends import BackendError, GameBackend, Option, Status
from ..core.events import Event, mask
from .wine import (
    ProcessQuery,
    WineBackend,
    path_option,
    profile_setting,
    scan_pids,
    stop_pids,
    stream,
)

WINE_SCRIPT_SUFFIXES = (".bat", ".cmd")


class _BatchWine(WineBackend):
    """The Wine backend, told which file the script backend resolved.

    A profile can hold ``launch_exe``, ``script`` and ``exe_name`` together; Wine and the script
    backend rank them differently, so a stale ``exe_name`` made a .bat profile run another file
    (#862). The delegate runs exactly what the script backend identified.
    """

    #: Script profiles offer no Sandbox switch, so a hand-edited key must not turn one on.
    supports_secure_mode = False

    def __init__(self, ctx: Any, chosen: Path) -> None:
        super().__init__(ctx)
        self._chosen = chosen

    def _target(self) -> Path:
        return self._chosen



def _own_groups(pids: list[int]) -> set[int]:
    """Process groups led by one of ``pids`` (never Nadare's own group)."""
    mine = os.getpgrp()
    groups: set[int] = set()
    for pid in pids:
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group == pid and group != mine:
            groups.add(group)
    return groups


class ScriptBackend(GameBackend):
    kind = "script"
    supports_update = False  # a script profile updates itself

    options = (
        Option(
            "launch_exe",
            "Script",
            "text",
            "Script to run; a bare name resolves against the game folder.",
        ),
        Option(
            "install_dir",
            "Game folder",
            "path_dir",
            "Base directory a relative script name resolves against.",
        ),
        Option(
            "prefix",
            "Wine prefix (.bat/.cmd)",
            "path_dir",
            "The Windows folder (a Wine prefix) a .bat or .cmd script runs in.",
        ),
        Option(
            "process_match",
            "Process match",
            "text",
            "Substring used to detect/stop the running script.",
            advanced=True,
        ),
    )

    # ---- profile resolution -----------------------------------------------
    def _script(self) -> Path:
        raw = (
            profile_setting(self.ctx.profile, "launch_exe")
            or profile_setting(self.ctx.profile, "script")
            or profile_setting(self.ctx.profile, "exe_name")
        )
        if not raw:
            raise BackendError("script profile has no launch_exe")
        base = path_option(profile_setting(self.ctx.profile, "install_dir")) or self.ctx.data_dir()
        script = path_option(raw, base=base)
        if script is None or not script.is_file():
            raise BackendError(f"script not found: {script}")
        return script

    def _is_batch(self) -> bool:
        try:
            return self._script().suffix.lower() in WINE_SCRIPT_SUFFIXES
        except BackendError:
            return False

    def _patterns(self, script: Path | None = None) -> ProcessQuery:
        """Match the resolved script as an exact argv element.

        A bare script *name* must never be a substring match: a shell or editor
        that merely mentions ``run.sh`` is not the game.
        """
        extra = profile_setting(self.ctx.profile, "process_match")
        if script is None:
            try:
                script = self._script()
            except BackendError:
                script = None
        args = (str(script),) if script is not None else ()
        return ProcessQuery(
            match=str(extra) if extra else "",
            args=args,
            arg_runners=("bash", "sh", "dash", "zsh", "fish", "ksh"),
        )

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        data: dict[str, object] = {"can_install": False, "can_update": False}
        configured = (
            profile_setting(self.ctx.profile, "launch_exe")
            or profile_setting(self.ctx.profile, "script")
            or profile_setting(self.ctx.profile, "exe_name")
        )
        if not configured:
            return Status(
                state="error", message="script profile has no launch_exe", data=data
            )
        try:
            script = self._script()
        except BackendError as exc:
            return Status(state="not_installed", message=str(exc), data=data)
        batch = self._is_batch()
        # A batch file runs under Wine (launch and stop delegate to _BatchWine): look for it
        # the way stop does, or a running batch game reads as installed (#973).
        query = _BatchWine(self.ctx, script)._patterns() if batch else self._patterns(script)
        pids = scan_pids(query)
        data.update({"script": str(script), "batch": batch, "pids": pids})
        return Status(
            state="running" if pids else "installed",
            running=bool(pids),
            data=data,
        )

    def install(self) -> None:
        raise BackendError("the script backend has nothing to install")

    def update(self) -> None:
        raise BackendError("the script backend has nothing to update")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        if self._is_batch():
            # A batch file needs the profile's wine/Proton; one code path only.
            return _BatchWine(self.ctx, self._script()).launch()
        script = self._script()
        argv = ["bash", str(script)]
        self.ctx.emit(Event("step", f"Script: launching {script.name}"))
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        extra_env = profile_setting(self.ctx.profile, "extra_env")
        extra = (
            {str(k): str(v) for k, v in extra_env.items()} if isinstance(extra_env, dict) else {}
        )
        # Nadare's own environment loses its credential-looking variables (#811); what the
        # profile sets on purpose is the user's choice and stays.
        env = process.drop_secret_env(process.build_env(None))
        env.update(extra)
        return stream(
            argv,
            env,
            on_line=lambda line: self.info(mask(line)),
            cancel=self.ctx.cancel,
            stop_wine=False,
            cwd=str(script.parent),  # relative paths in the script mean its own folder
        )

    def stop(self) -> None:
        if self._is_batch():
            _BatchWine(self.ctx, self._script()).stop()
            return
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            # Nadare starts the script in its own session, so what the script started shares its
            # group; signalling only the shell would leave the game running (#932).
            groups = _own_groups(pids)
            stop_pids(pids)
            for group in groups:
                try:
                    os.killpg(group, signal.SIGTERM)
                except OSError:
                    pass

    def verify(self) -> dict:
        try:
            script = self._script()
        except BackendError as exc:
            return {"verified": False, "message": str(exc)}
        return {"verified": True, "script": str(script)}
