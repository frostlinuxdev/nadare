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
    monitors,
    process,
    protoncmd,
    runlock,
    sandbox,
    shadercache,
    vcredist,
)
from ....core import runners as core_runners
from ....core.backends import (
    BackendError,
    BackendNotInstalled,
)
from ....core.events import Event
from ...wine import ProcessQuery, scan_pids, stop_elsewhere, stop_runner_wineserver
from .runner import DEFAULT_RUNNER
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

#: Play refuses with this when WebView2 is missing or half installed (no download at Play).
WEBVIEW2_MISSING_AT_PLAY = (
    "Microsoft WebView2 (needed for sign-in) is missing or broken in this game's prefix. "
    "Press Repair files to put it back."
)

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


def game_pids(game_dir: Path, *, helpers: bool = True) -> list[int]:
    """This user's processes running an exe from ``game_dir``'s ``bin`` (Wine shows it as
    ``Z:\\<unix path with backslashes>``); ``helpers=False`` leaves out the crash handler.

    CZN's one process check: the backend and the updater (``update.py``) both ask here (#1557).
    """
    folders = {Path(game_dir)}
    # Flatpak exposes symlink targets and rewrites the launch path to match; native
    # launches may still use the configured alias. Both identify this same game.
    with contextlib.suppress(OSError, RuntimeError):
        folders.add(Path(game_dir).resolve())
    prefixes = [
        ("Z:" + str(folder / "bin").replace("/", "\\") + "\\").lower()
        for folder in folders
    ]
    uid = os.getuid()
    pids: list[int] = []
    exclude = frozenset() if helpers else GAME_HELPER_EXES
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
        if any(_cmdline_has_exe(raw, prefix, exclude=exclude) for prefix in prefixes):
            pids.append(int(d.name))
    return pids


def running_elsewhere(prefix: Path) -> bool:
    """Another Flatpak instance launched this prefix's game."""
    return sandbox.in_flatpak() and runlock.running(prefix)


def game_running(prefix: Path, game_dir: Path) -> bool:
    """The game runs (here, or from another Flatpak window): the rule ``CznBackend._running``
    applies, for callers without a backend object (the updater, #1557)."""
    return bool(game_pids(game_dir, helpers=False)) or running_elsewhere(prefix)


class LaunchingMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    # ---- running-state -----------------------------------------------------
    def _game_pids(self, *, helpers: bool = True) -> list[int]:
        return game_pids(self.game_dir(), helpers=helpers)

    def _game_running(self) -> bool:
        """The game itself is running (a lingering crash handler is not)."""
        return bool(self._game_pids(helpers=False))

    def _running_elsewhere(self) -> bool:
        """Another Flatpak instance launched this prefix's game."""
        return running_elsewhere(self.prefix())

    def _running(self) -> bool:
        return self._game_running() or self._running_elsewhere()

    # ---- launch ------------------------------------------------------------
    def _prefix_command(
        self,
        args: list[str],
        extra_env: dict[str, str] | None = None,
        *,
        sandboxed: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the umu-run command and environment for running `args` in this profile's
        prefix with the selected runner.

        The umu decision and the Play speed-ups are the ones every Proton game shares
        (:mod:`nadare.core.protoncmd`, #1449); what stays here is CZN's own: opt-in GL
        threading, the ``pfx`` link and ``GAMEID=0``. CZN always runs on Proton
        (``resolve_runner`` never answers plain Wine, #1310 W9-1).

        ``sandboxed`` says the command will run in the Sandbox: inside the Flatpak that means
        Proton runs directly, because umu needs the session bus the Sandbox hides.
        """
        prefix = self.prefix()
        proton, _kind = self.resolve_runner()
        env = process.build_env(
            {"pass_env": _profile_setting(self.ctx.profile, "pass_env")}
        )
        env["WINEPREFIX"] = str(prefix)
        env.update(graphics.renderer_env(self.ctx.profile, kind="proton", environ=env))
        # CZN is an OpenGL game whose render thread is CPU-bound under Wine. Driver-side
        # GL threading looked helpful on one AMD box (p99 ~140 -> ~50 ms) but the A/B was
        # not controlled, so it is opt-in: ``gl_threading: true`` in the profile.
        if self.ctx.profile.get("gl_threading", False) is True:
            env.setdefault("mesa_glthread", "true")  # Mesa (AMD/Intel/nouveau)
            env.setdefault("__GL_THREADED_OPTIMIZATIONS", "1")  # NVIDIA proprietary
        monitors.apply_env(env, self.ctx)
        shadercache.prepare(self.ctx.name, env, self.info, runner=proton)
        profile_env = _profile_setting(self.ctx.profile, "extra_env")
        if isinstance(profile_env, dict):
            process.apply_extra_env(env, profile_env, reserved=process.LAUNCH_RESERVED_ENV)
        process.apply_extra_env(env, extra_env, reserved=process.LAUNCH_RESERVED_ENV)
        # umu treats the prefix as Proton's compat-data dir and expects the
        # Wine prefix at <prefix>/pfx; a symlink pfx -> . lets one dir serve both.
        pfx = prefix / "pfx"
        if not pfx.exists():
            try:
                pfx.symlink_to(".", target_is_directory=True)
            except OSError:
                pass
        env["PROTONPATH"] = str(proton)
        env.setdefault("GAMEID", "0")
        protoncmd.note_prefix_upgrade(
            self, prefix, proton, name="Chaos Zero Nightmare", emit=self.ctx.emit
        )
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
        argv = protoncmd.proton_argv(
            args,
            proton=proton,
            prefix=prefix,
            env=env,
            configured=str(_profile_setting(self.ctx.profile, "umu_binary") or "umu-run"),
            sandboxed=sandboxed,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )
        protoncmd.play_speedups(env, prefix, proton=True, info=self.info)
        shadercache.log_caches(
            self.ctx.name,
            prefix,
            env,
            lambda line: self.info(line),
            sandboxed=sandboxed,
        )
        return argv, process.sanitize_game_env(
            env, preserve=process.launchenv.explicit_names(self.ctx.profile, extra_env)
        )

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

    def _run_unwrapped(self, argv: list[str], env: dict[str, str], *, on_line) -> int:  # noqa: ANN001
        """A setup step outside the Sandbox (core/sandboxed.py asks for umu's runtime first)."""
        return self._stream(argv, env)

    def launch(self) -> int:
        with self._run_lock():
            return self._launch()

    def _run_lock(self) -> contextlib.AbstractContextManager[None]:
        """Inside the Flatpak, let other Nadare instances see this launch and ask it to stop."""
        if not sandbox.in_flatpak():
            return contextlib.nullcontext()
        return runlock.guard(self.prefix(), self._stop_from_elsewhere)

    def _stop_from_elsewhere(self) -> None:
        """Another window asked to stop the game: end its processes, then the wait.

        Cancelling alone ends only the launch wrapper; a Sandboxed game keeps running (the same
        finding as WineGame's, #1455 R4-2)."""
        self._kill_game()
        self.ctx.cancel.cancel()

    def _launch(self) -> int:
        if not self.installed():
            raise BackendNotInstalled("the game is not installed", 1)
        # The run lock this launch holds itself would look like another instance.
        if self._game_running():
            self._fail("launch", 8)
        # Without WebView2 the game shows only its own "run through the launcher" error.
        # A file + registry read (no Wine, no download): Repair files puts it back.
        if not self._webview2_installed():
            raise BackendError(WEBVIEW2_MISSING_AT_PLAY, 1)
        # Opt-in for CZN (#1556): False unless the profile switched the Sandbox on.
        sandboxed = self.secure_mode_enabled()
        if sandboxed and not vcredist.installed(self.prefix()):
            raise BackendError(
                "Microsoft Visual C++ runtime is missing or broken in this game's prefix. "
                "Press Repair files to put it back."
            )
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
            # Prefix installers run during Install/Repair. Starting one here would leave
            # a second wineserver outside the game's private /tmp and PID namespace.
            if not sandboxed:
                self._ensure_vcredist()
            log = self._stub_log(sandboxed=sandboxed)
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
            full, env = self._prefix_command(cmd, sandboxed=sandboxed)
            # Set after _prefix_command: its env cleanup drops every CZN_* name, and
            # without this path the stub never logs, so the game never looks started.
            env["CZN_STUB_LOG"] = self._win(log)
            full = self._mangohud_wrap(full, env)
            # The stub and the one-time session file live outside the prefix: bind those two
            # files read only, never the folder that holds other profiles' secrets. The stub
            # starts the game and WebView2, so one wrapper covers every Play-time process.
            full, env = self._secure_wrap(full, env, read_only=[stub, session])
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
            self._stop_link_relay()
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
            # session.json holds a live game token; it must not outlive the run. The stub
            # reads and deletes it at start (#811); this covers a stub that never started.
            session.unlink(missing_ok=True)
            self._stop_link_relay()
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
        pids = self._stop_game_pids()
        if not pids and self._running_elsewhere():
            stop_elsewhere(self.prefix(), self.info)
            return
        self._kill_game(pids)
        # Like every Wine game: the prefix's helpers go too (#1455 R4-2; CZN never did this).
        stop_runner_wineserver(prefix=self.prefix(), proton=self._runner_for_stop())

    def _stop_game_pids(self) -> list[int]:
        pids = self._game_pids()
        if self.secure_mode_enabled():
            # WebView2 and Wine services live outside the game's bin folder. The host's
            # wineserver -k cannot reach their server in the Sandbox's private /tmp.
            pids = sorted(set(pids).union(scan_pids(ProcessQuery(prefix=str(self.prefix())))))
        return pids

    def _runner_for_stop(self) -> str | None:
        """This profile's Proton, only if already installed: Stop never downloads one."""
        rid = core_runners.profile_runner_value(self.ctx.profile)
        if not rid or rid.lower() in ("wine", "system", "none"):
            rid = DEFAULT_RUNNER
        try:
            if rid in core_runners.RUNNERS:
                found = self._installed_pinned(rid)
            else:
                found = core_runners.resolve_runner(rid)
        except Exception:  # noqa: BLE001 - Stop must not fail on a runner lookup
            return None
        return str(found) if found else None

    def _kill_game(self, pids: list[int] | None = None) -> None:
        """SIGTERM the game's processes, wait up to 10 s, then SIGKILL what is left."""
        if pids is None:
            pids = self._stop_game_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 10
        while time.time() < deadline and self._game_running():
            time.sleep(0.25)
        for pid in self._stop_game_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
