# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Building the launch command, starting the game through the stub and stopping it.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path

from ....core import (
    graphics,
    hostlibs,
    monitors,
    process,
    runlock,
    sandbox,
)
from ....core import umu as umu_mod
from ....core.backends import (
    BackendError,
)
from ....core.events import Event
from .shared import (
    UPDATE_MODULE,
    _profile_setting,
    _register_ephemeral,
)

log = logging.getLogger("nadare.backends.czn.backend")

#: Environment for silent installers run through Proton. The native Wayland
#: driver makes the WebView2 installer exit silently (exit 8, nothing installed)
#: on some setups; an installer needs no window, so it always runs via XWayland.
_INSTALLER_ENV = process.installer_env()

_RESULT = re.compile(r"RESULT\s+([a-z-]+)")

#: Emitted once when "Remember me" is on but no OS keyring is available.
#: Exit codes a game child reports when it was terminated on purpose
#: (-SIGTERM, 128+SIGTERM, and the 256-15 wrap umu/proton produce; likewise SIGINT).
_STOPPED_EXIT_CODES = frozenset({-15, 143, 241, -2, 130, 254})

#: Helper processes the game leaves around; they are not "the game running"
#: (a crash handler can linger after the game has exited) but Stop kills them.
GAME_HELPER_EXES = frozenset({"crashpad_handler.exe"})


def _cmdline_has_exe(raw: bytes, prefix: str, *, exclude: frozenset[str] = frozenset()) -> bool:
    """Whether any NUL-separated argv token names an exe under ``prefix``.

    Matching whole argv tokens instead of the raw cmdline stops an unrelated
    process that merely mentions the path inside one argument (an editor, a
    log, ``ps``) from being mistaken for the game. Tokens whose file name is in
    ``exclude`` are ignored."""
    for token in raw.split(b"\0"):
        if not token:
            continue
        text = token.decode("utf-8", "replace").lower()
        if not text.startswith(prefix):
            continue
        if exclude and text.replace("/", "\\").rsplit("\\", 1)[-1] in exclude:
            continue
        return True
    return False


class LaunchingMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    # ---- running-state -----------------------------------------------------
    def _game_pids(self, *, helpers: bool = True) -> list[int]:
        prefix = ("Z:" + str(self.game_dir() / "bin").replace("/", "\\") + "\\").lower()
        uid = os.getuid()
        pids: list[int] = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                # Only processes owned by this user can be the game; this stops
                # another user from planting a matching argv and being counted.
                if d.stat().st_uid != uid:
                    continue
                raw = (d / "cmdline").read_bytes()
            except OSError:
                continue
            exclude = frozenset() if helpers else GAME_HELPER_EXES
            if _cmdline_has_exe(raw, prefix, exclude=exclude):
                pids.append(int(d.name))
        return pids

    def _game_running(self) -> bool:
        """The game itself is running (a lingering crash handler is not)."""
        return bool(self._game_pids(helpers=False))

    def _running_elsewhere(self) -> bool:
        """Another Flatpak instance launched this prefix's game."""
        return sandbox.in_flatpak() and runlock.running(self.prefix())

    def _running(self) -> bool:
        return self._game_running() or self._running_elsewhere()

    # ---- launch ------------------------------------------------------------
    def _prefix_command(
        self, args: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Build the umu-run/wine command and environment for running `args` in
        this profile's prefix with the selected runner."""
        prefix = self.prefix()
        proton, kind = self.resolve_runner()
        env = self._env()
        env["WINEPREFIX"] = str(prefix)
        env.update(graphics.renderer_env(self.ctx.profile, kind=kind, environ=env))
        # CZN is an OpenGL game whose render thread is CPU-bound under Wine. Driver-side
        # GL threading looked helpful on one AMD box (p99 ~140 -> ~50 ms) but the A/B was
        # not controlled, so it is opt-in: ``gl_threading: true`` in the profile.
        if self.ctx.profile.get("gl_threading", False) is True:
            env.setdefault("mesa_glthread", "true")  # Mesa (AMD/Intel/nouveau)
            env.setdefault("__GL_THREADED_OPTIMIZATIONS", "1")  # NVIDIA proprietary
        monitors.apply_env(env, self.ctx)
        profile_env = self.ctx.profile.get("extra_env")
        if isinstance(profile_env, dict):
            process.apply_extra_env(env, profile_env, reserved=process.LAUNCH_RESERVED_ENV)
        if extra_env:
            env.update(extra_env)
        if kind == "proton":
            umu = umu_mod.system()
            if umu is None and not sandbox.in_flatpak():
                missing = self._missing_hostlibs()
                if missing:
                    umu = umu_mod.resolve(
                        "umu-run",
                        allow_download=True,
                        emit=self.ctx.emit,
                        cancel=self.ctx.cancel,
                    )
            # umu treats the prefix as Proton's compat-data dir and expects the
            # Wine prefix at <prefix>/pfx; a symlink pfx -> . lets one dir serve both.
            pfx = prefix / "pfx"
            if not pfx.exists():
                try:
                    pfx.symlink_to(".", target_is_directory=True)
                except OSError:
                    pass
            env["PROTONPATH"] = proton
            env.setdefault("GAMEID", "0")
            # A Display choice beats an exported PROTON_ENABLE_WAYLAND; a profile
            # extra_env value still wins, as it does for the other Wine backends.
            choice = str(_profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
            if not (isinstance(profile_env, dict) and "PROTON_ENABLE_WAYLAND" in profile_env):
                if choice in ("x11", "xwayland", "wayland"):
                    env["PROTON_ENABLE_WAYLAND"] = self._wayland_default()
                else:
                    env["PROTON_ENABLE_WAYLAND"] = os.environ.get(
                        "PROTON_ENABLE_WAYLAND", self._wayland_default()
                    )
            if extra_env and "PROTON_ENABLE_WAYLAND" in extra_env:
                env["PROTON_ENABLE_WAYLAND"] = extra_env["PROTON_ENABLE_WAYLAND"]
            env.pop("UMU_NO_PROTON", None)
            if umu:
                return [umu, *args], process.sanitize_game_env(env)
            # No umu-run (or inside Flatpak): run the Proton tree directly, so
            # umu-launcher is not a hard dependency.
            if not (Path(proton) / "proton").is_file():
                raise BackendError(
                    "the selected runner needs umu-run (umu-launcher): install "
                    "umu-launcher, or choose a Proton runner that does not need it "
                    "in Settings",
                    1,
                )
            # Running a Steam-Runtime Proton build directly makes it load the
            # host's 32-bit libraries; without them wineboot cannot find
            # FreeType and the WebView2 installer hangs silently. Fail fast
            # (once per backend) unless we are inside the Flatpak runtime, which
            # ships its own libraries.
            if not sandbox.in_flatpak():
                missing = self._missing_hostlibs()
                if missing:
                    raise BackendError(hostlibs.hint(missing), 1)
            argv, env_updates = sandbox.proton_direct(proton, prefix, args)
            env.update(env_updates)
            return argv, process.sanitize_game_env(env)
        wine = os.environ.get("CZN_WINE") or shutil.which("wine")
        if not wine:
            raise BackendError(
                "no Proton runner found: pick one in Settings -> Manage runners "
                "(CZN_WINE also works for advanced use)",
                1,
            )
        # A fresh plain-Wine prefix otherwise pops "Wine could not find a
        # wine-mono / wine-gecko package" dialogs. The game and WebView2 need
        # neither, so suppress both prompts (Proton handles this itself).
        parts = [p for p in str(env.get("WINEDLLOVERRIDES") or "").split(";") if p.strip()]
        if "mscoree,mshtml=" not in parts:
            parts.append("mscoree,mshtml=")
        env["WINEDLLOVERRIDES"] = ";".join(parts)
        return [wine, *args], process.sanitize_game_env(env)

    def _missing_hostlibs(self) -> list[str] | None:
        """The missing host 32-bit sonames, checked at most once per object.

        Honours ``NADARE_SKIP_HOSTLIBS_CHECK=1`` as an escape hatch, and returns
        ``None`` when the check is skipped or inconclusive. Never raises.
        """
        if not self._hostlibs_checked:
            self._hostlibs_checked = True
            if os.environ.get("NADARE_SKIP_HOSTLIBS_CHECK") != "1":
                self._hostlibs_result = hostlibs.missing_32bit()
        return self._hostlibs_result

    def _wayland_default(self) -> str:
        """``1`` (native Wayland) unless the profile asks for X11/XWayland."""
        choice = str(_profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        return "0" if choice in ("x11", "xwayland") else "1"

    def _mangohud_wrap(self, full: list[str], env: dict[str, str]) -> list[str]:
        """Prefix the game command with ``mangohud`` when ``NADARE_MANGOHUD=1``
        asks for the FPS overlay and it is installed.

        The old per-profile ``mangohud`` key is deliberately ignored: older
        betas could leave it on with no Settings control to turn it off, and
        MangoHud hangs CZN under ``umu-run``.
        """
        if os.environ.get("NADARE_MANGOHUD") != "1":
            return full
        binary = shutil.which("mangohud")
        if not binary:
            self.info("MangoHud is enabled for this profile but is not installed")
            return full
        env.setdefault("MANGOHUD", "1")
        env.setdefault(
            "MANGOHUD_CONFIG", "fps,frametime,frame_timing,gpu_stats,cpu_stats,position=top-left"
        )
        # --dlsym makes MangoHud hook GL/EGL through dlsym, which a Wine/Proton
        # OpenGL game needs.
        return [binary, "--dlsym", *full]

    @contextlib.contextmanager
    def _activity_label(self, label: str):
        """Name the silent work inside the block so ``_stream`` can show it is alive."""
        previous = self._activity
        self._activity = label
        try:
            yield
        finally:
            self._activity = previous

    @contextlib.contextmanager
    def _installer_environment(self):
        """Run prefix commands made inside the block as silent installers.

        They get ``_INSTALLER_ENV`` (no native Wayland: an installer needs no
        window and the Wayland driver breaks the WebView2 one).
        """
        previous = self._run_env
        self._run_env = _INSTALLER_ENV
        try:
            yield
        finally:
            self._run_env = previous

    def _run_in_prefix(self, args: list[str]) -> int:
        cmd, env = self._prefix_command(args, self._run_env)
        cwd = self.game_dir() if self.game_dir().is_dir() else self.prefix()
        return self._stream(cmd, env, cwd=cwd)

    def launch(self) -> int:
        with self._run_lock():
            return self._launch()

    def _run_lock(self) -> contextlib.AbstractContextManager[None]:
        """Inside the Flatpak, let other Nadare instances see this launch and ask it to stop."""
        if not sandbox.in_flatpak():
            return contextlib.nullcontext()
        return runlock.guard(self.prefix(), self.ctx.cancel.cancel)

    def _launch(self) -> int:
        if not self.installed():
            raise BackendError("the game is not installed", 1)
        # The run lock this launch holds itself would look like another instance.
        if self._game_running():
            self._fail("launch", 8)
        if os.environ.get("CZN_SKIP_UPDATE") != "1":
            code = self._run_module(UPDATE_MODULE, ["update", "--apply"])
            if code == 3:
                self._fail("launch", 8)
            if code == 2:
                self.info("could not check for updates (offline?); launching the installed version")
            elif code != 0:
                self._fail("launch update", 5)
        # Restore a remembered/live session once, then mint the game token
        # right before launch so session.json is fresh and short-lived. Clear a
        # stale session only immediately before minting (a session we already
        # minted in this process must survive).
        if not self._restore_attempted:
            self._cleanup_stale_session()
            if not self.restore():
                self._raise_login_required()
        else:
            # A login in this process set the token, or an earlier launch
            # already restored it; (re-)mint the game session right before
            # every launch, since the previous run deletes session.json on exit.
            self._cleanup_stale_session()
            if not self._refresh_token:
                self._raise_login_required()
            auth_code = self._mint_session()
            if auth_code != 0:
                self._fail_auth("token refresh", auth_code)
        session = self._session()
        try:
            stub = self._ensure_stub()
            self._ensure_vcredist()
            log = self._stub_log()
            log.unlink(missing_ok=True)
            session.parent.mkdir(parents=True, exist_ok=True)
            _register_ephemeral(session)
            self.ctx.emit(Event("step", "Starting Chaos Zero Nightmare…"))
            cmd = [
                str(stub),
                self._win(session),
                "--launch",
                self._win(self.shield()),
                self._win_cwd(self.game_dir()),
            ]
            full, env = self._prefix_command(cmd)
            # Set after _prefix_command: its env cleanup drops every CZN_* name, and
            # without this path the stub never logs, so the game never looks started.
            env["CZN_STUB_LOG"] = self._win(log)
            full = self._mangohud_wrap(full, env)
            cwd = self.game_dir() if self.game_dir().is_dir() else self.prefix()
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = graphics.RendererProbe(
                self._game_pids, on_result=lambda label: self.info(f"renderer: {label}")
            ).start()
        except BaseException:
            # The minted game token must not outlive a failed pre-launch step.
            session.unlink(missing_ok=True)
            raise
        try:
            with self._activity_label("Starting the game"):
                code = self._stream(
                    full,
                    env,
                    cwd=cwd,
                    kill_game=True,
                    beat_as_log=True,
                    beat_until=lambda: self._stub_connected(log),
                )
        finally:
            # session.json holds a live game token; it must not outlive the run. The Go
            # stub already read and deleted it at start (#811); this covers the C stub.
            session.unlink(missing_ok=True)
            probe.stop()
            probe.join(timeout=1.0)
        self._renderer = probe.result
        result = self._stub_result(log)
        if result == "ok":
            self.ctx.emit(Event("done", "Game exited normally"))
            return 0
        if result == "no-connect":
            self._fail("launch", 6)
        if result == "handshake-rejected":
            self._fail("launch", 7)
        if code in _STOPPED_EXIT_CODES:
            # SIGTERM/SIGINT through umu/proton (``nadare stop``, or the user closing
            # the window): a deliberate stop, not a failed launch.
            self.ctx.emit(Event("done", "Game stopped"))
            return 0
        self._fail("launch", code or 1)
        return code  # unreachable

    @staticmethod
    def _stub_result(log: Path) -> str | None:
        try:
            m = None
            for line in log.read_text(errors="replace").splitlines():
                hit = _RESULT.search(line)
                if hit:
                    m = hit.group(1)
            return m
        except OSError:
            return None

    def stop(self) -> None:
        pids = self._game_pids()
        if not pids:
            if self._running_elsewhere():
                self.info("asking the other Nadare window to stop the game")
                runlock.request_stop(self.prefix())
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and runlock.running(self.prefix()):
                    time.sleep(0.2)
                if runlock.running(self.prefix()):
                    self.info("the game did not stop within 20 s")
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 10
        while time.time() < deadline and self._game_running():
            time.sleep(0.25)
        for pid in self._game_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
