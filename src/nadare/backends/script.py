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

import shlex
from pathlib import Path

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


class ScriptBackend(GameBackend):
    kind = "script"

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
        return ProcessQuery(match=str(extra) if extra else "", args=args)

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        try:
            script = self._script()
        except BackendError as exc:
            return Status(state="not_installed", message=str(exc))
        pids = scan_pids(self._patterns(script))
        return Status(
            state="running" if pids else "installed",
            running=bool(pids),
            data={"script": str(script), "batch": self._is_batch(), "pids": pids},
        )

    def install(self) -> None:
        raise BackendError("the script backend has nothing to install")

    def update(self) -> None:
        raise BackendError("the script backend has nothing to update")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        if self._is_batch():
            # A batch file needs the profile's wine/Proton; one code path only.
            return WineBackend(self.ctx).launch()
        script = self._script()
        argv = ["bash", str(script)]
        self.ctx.emit(Event("step", f"Script: launching {script.name}"))
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        extra_env = profile_setting(self.ctx.profile, "extra_env")
        env = process.build_env(
            None,
            extra=(
                {str(k): str(v) for k, v in extra_env.items()}
                if isinstance(extra_env, dict)
                else None
            ),
        )
        return stream(
            argv,
            env,
            on_line=lambda line: self.info(mask(line)),
            cancel=self.ctx.cancel,
            stop_wine=False,
        )

    def stop(self) -> None:
        if self._is_batch():
            WineBackend(self.ctx).stop()
            return
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            stop_pids(pids)

    def verify(self) -> dict:
        try:
            script = self._script()
        except BackendError as exc:
            return {"verified": False, "message": str(exc)}
        return {"verified": True, "script": str(script)}
