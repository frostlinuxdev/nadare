# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Running a helper module and turning its output into events and errors.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ....core.backends import (
    BackendError,
)
from ....core.events import Cancelled, Event, mask
from .shared import (
    AUTH_EXIT_MESSAGES,
)

log = logging.getLogger("nadare.backends.czn.backend")

#: Seconds of silence from a long-running child before a "still working" line.
HEARTBEAT_SECONDS = 15.0

#: czn-linux subprocess exit codes -> a user-safe message. The backend-side
#: WebView2 codes live in their own map (below) so a helper that later returns
#: 10/11 cannot surface a WebView2-specific message for an unrelated failure.
EXIT_MESSAGES: dict[int, str] = {
    1: "setup or usage error",
    2: "cannot reach the update server",
    3: "the STOVE login expired — log in again",
    4: "cannot reach STOVE (network)",
    5: "the game update failed",
    6: "the game exited without contacting the launcher stub (the PCSDK "
    "handshake may have changed, or this runner cannot run the game)",
    7: "the game rejected the login session",
    8: "the game is already running",
    9: "a runner download failed its checksum",
}

#: auth.py ``qr_status`` states grouped into the coarse state the GUI renders.
_QR_STATE = {
    "init": "waiting",
    "ongoing": "waiting",
    "success": "approved",
    "expired": "expired",
    "close": "expired",
    "closed": "expired",
    "cancel": "expired",
    "cancelled": "expired",
}

_QR_MESSAGE = {
    "waiting": "Waiting for you to approve the scan in the STOVE app…",
    "approved": "Approved — finishing login…",
    "expired": "QR code expired — log in again for a fresh code.",
}

_FILE_PROGRESS = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*(.*)$")


class StreamingMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    def _fail(
        self,
        what: str,
        code: int,
        messages: dict[int, str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        table = EXIT_MESSAGES if messages is None else messages
        raise BackendError(table.get(code, f"{what} failed (exit {code})"), code) from cause

    def _fail_auth(self, what: str, code: int) -> None:
        """Report an auth-worker failure, keeping its real message when useful."""
        if code == 2 and self._last_auth_error:
            raise BackendError(
                f"STOVE returned an unexpected error: {self._last_auth_error}. "
                "Try again in a minute; if it keeps happening, open the logs.",
                code,
            )
        self._fail(what, code, AUTH_EXIT_MESSAGES)

    # ---- event plumbing ----------------------------------------------------
    def _emit_json_event(self, obj: dict[str, Any]) -> None:
        name = obj.get("event")
        if name == "qr":
            self.ctx.emit(
                Event(
                    "step",
                    "Scan the QR code with the STOVE app",
                    data={
                        "png": obj.get("png"),
                        "url": obj.get("url"),
                        "timeout": obj.get("timeout"),
                    },
                )
            )
        elif name == "qr_status":
            state = str(obj.get("state") or "")
            qr_state = _QR_STATE.get(state, "waiting")
            self.ctx.emit(
                Event(
                    "step",
                    _QR_MESSAGE[qr_state],
                    data={"qr_state": qr_state, "state": state},
                )
            )
        elif name == "logged_in":
            self.ctx.emit(Event("step", "Logged in to STOVE"))
        elif name == "credential":
            # Internal handoff only: the launcher refresh token must never
            # reach the UI. The backend reads it from the 0600 handoff file.
            return
        elif name == "session":
            self.ctx.emit(
                Event(
                    "step",
                    "Game token ready",
                    data={
                        "path": obj.get("path"),
                        "expires_at": obj.get("expires_at"),
                        "expires_in": obj.get("expires_in"),
                    },
                )
            )
        elif name == "status":
            self.ctx.emit(Event("status", "STOVE status", data=obj))
        elif name == "progress":
            done, total = obj.get("done"), obj.get("total")
            frac = (
                (done / total)
                if isinstance(done, int) and isinstance(total, int) and total
                else None
            )
            self.ctx.emit(
                Event(
                    "progress",
                    str(obj.get("stage", "download")),
                    progress=frac,
                    data={"done": done, "total": total},
                )
            )
        elif name == "installed":
            self.ctx.emit(
                Event(
                    "step",
                    f"Runner installed: {obj.get('tag', '')}",
                    data={"path": obj.get("path"), "tag": obj.get("tag")},
                )
            )
        elif name == "error":
            message = mask(str(obj.get("message", "")))
            # ``_run_auth`` uses this to tell an expired QR / rejected sign-in
            # (exit 3) from a STOVE/API error (exit 2) in the final dialog.
            self._last_auth_error = message
            self.ctx.emit(Event("error", message, data={"kind": obj.get("kind")}))
        else:
            self.info(mask(str(obj)))

    def _emit_text(self, line: str) -> None:
        line = line.rstrip()
        if not line.strip():
            return
        self._watch.feed(line)
        m = _FILE_PROGRESS.match(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            self.ctx.emit(
                Event(
                    "progress",
                    line.strip(),
                    progress=(done / total if total else None),
                    data={"done": done, "total": total},
                )
            )
            return
        tag = line.lstrip()[:3]
        kind = "step" if tag in ("[*]", "[+]") else ("error" if tag == "[!]" else "log")
        self.ctx.emit(Event(kind, mask(line.strip())))

    def _explain(self, message: str) -> str:
        """Add the userns and/or failed-umu-setup hint to a failure message."""
        return self._watch.explain(message)

    def _stream(
        self,
        cmd: list[str],
        env: dict[str, str],
        *,
        parse_json: bool = False,
        collect: list[dict[str, Any]] | None = None,
        cwd: Path | None = None,
        kill_game: bool = False,
        beat_as_log: bool = False,
        beat_until: Callable[[], bool] | None = None,
    ) -> int:
        """Run ``cmd``, streaming its output as events.

        Under :meth:`_activity_label` a silent child gets a heartbeat line every
        ``HEARTBEAT_SECONDS``. ``beat_as_log`` emits plain log lines instead of
        busy-bar progress events (the game page must not show a busy bar while
        a game runs); ``beat_until`` stops the beats once it returns true.
        """
        self.ctx.emit(Event("log", "$ " + " ".join(cmd)))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
                cwd=str(cwd) if cwd else None,
                start_new_session=True,
            )
        except OSError as e:
            raise BackendError(f"cannot start {cmd[0]!r}: {e}") from e
        if proc.stdout is None:
            raise BackendError("child process has no stdout pipe")
        fd = proc.stdout.fileno()
        pending = b""
        label = self._activity
        started = last_beat = time.monotonic()
        if label and not beat_as_log:
            # A bar that visibly moves, so a silent installer is not mistaken for a hang.
            self.ctx.emit(
                Event("progress", f"{label}…", data={"indeterminate": True})
            )
        try:
            # Poll with select so Cancel is honoured while the child is silent
            # (a running game prints nothing). Raw reads keep select reliable:
            # a buffered text wrapper can hold lines select cannot see.
            while True:
                if self.ctx.cancel.cancelled:
                    self._abort_child(proc, kill_game=kill_game)
                    raise Cancelled()
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not ready:
                    # Plain Wine leaves wineserver/service processes running
                    # that inherit our pipe, so EOF may never come. Once the
                    # process we started has exited we are done.
                    if proc.poll() is not None:
                        break
                    now = time.monotonic()
                    if label and now - last_beat >= HEARTBEAT_SECONDS:
                        last_beat = now
                        if beat_until is not None and beat_until():
                            label = None  # up and running: no more "still starting" lines
                        elif beat_as_log:
                            self.ctx.emit(
                                Event(
                                    "log",
                                    f"{label}… {int(now - started)} s "
                                    "(the first launch through umu downloads the Steam "
                                    "Linux Runtime and can take a few minutes)",
                                )
                            )
                        else:
                            self.ctx.emit(
                                Event(
                                    "progress",
                                    f"{label}… {int(now - started)} s "
                                    "(the installer prints nothing while it works)",
                                    data={"indeterminate": True},
                                )
                            )
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break  # EOF: every writer closed the pipe
                last_beat = time.monotonic()
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    self._handle_line(
                        raw.decode("utf-8", "replace").rstrip("\r"), parse_json, collect
                    )
            if pending:
                tail = pending.decode("utf-8", "replace").rstrip("\r")
                self._handle_line(tail, parse_json, collect)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        return proc.wait()

    def _handle_line(
        self, line: str, parse_json: bool, collect: list[dict[str, Any]] | None
    ) -> None:
        if parse_json and line.lstrip().startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and "event" in obj:
                if collect is not None:
                    collect.append(obj)
                self._emit_json_event(obj)
                return
        self._emit_text(line)

    def _abort_child(self, proc: subprocess.Popen, *, kill_game: bool = False) -> None:
        """Stop a cancelled child *and* the game it started.

        ``proton run`` hands the real game to a wineserver that lives in its own
        session, so killing the wrapper's process group alone leaves it running.
        """
        self._kill(proc)
        if not kill_game:
            return
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - cancelling must always finish
            pass

    def _run_module(
        self,
        module: str,
        args: list[str],
        *,
        parse_json: bool = False,
        collect: list[dict[str, Any]] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> int:
        env = self._env()
        if extra_env:
            env.update(extra_env)
        return self._stream(
            [sys.executable, "-m", module, *args],
            env,
            parse_json=parse_json,
            collect=collect,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
