# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Building the launch command, starting the game and stopping it.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from ...core import (
    graphics,
    launch_window,
    monitors,
    process,
    protoncmd,
    runlock,
    runners,
    runtimewatch,
    sandbox,
    shadercache,
    stopstate,
    vcredist,
    x11windows,
)
from ...core.backends import (
    BackendError,
    BackendNotInstalled,
)
from ...core.config import as_bool
from ...core.events import Event
from ..wine import (
    cmdline_argv,
    first_wine,
    path_option,
    profile_setting,
    scan_pids,
    stop_elsewhere,
    stop_pids,
    stop_runner_wineserver,
    stream,
)

log = logging.getLogger("nadare.backends.winegame")

#: Bounded discovery: how deep and how many directories to walk in the prefix.
_SEARCH_MAX_DEPTH = 4

_SEARCH_MAX_DIRS = 2000

#: How long Stop asks the game's windows to close before it uses SIGTERM (#1421 ask 3).
STOP_GRACE_SECONDS = 8.0
#: A single ``taskkill`` helper may take this long before Stop goes on to SIGTERM.
_STOP_HELPER_TIMEOUT = 5.0
#: How often the polite stop looks for the game's processes while it waits.
_STOP_POLL_SECONDS = 0.5

#: Wine's own long-lived helpers. They belong to the prefix but are not the game, so
#: they must not keep the grace running after the game and its launcher have closed.
_WINE_HELPERS = frozenset(
    {"wineserver", "services", "winedevice", "plugplay", "rpcss", "tabtip", "conhost"}
)


def _wine_helper(pid: int) -> bool:
    """Whether ``pid`` is one of Wine's long-lived helper processes, not the game."""
    argv = cmdline_argv(Path("/proc") / str(pid)) or []
    if not argv:
        return False
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in _WINE_HELPERS:
        return True
    # Wine's desktop shell is a helper too; explorer.exe without /desktop is not used.
    return name == "explorer" and any(arg.lower().startswith("/desktop") for arg in argv[1:])


#: Kept names: the shared versions live in :mod:`nadare.core.protoncmd` (#1449).
_umu_unavailable = protoncmd.unavailable_message
_refuse_if_userns_blocked = protoncmd.refuse_if_userns_blocked

#: What this process's current launch left on disk for each prefix: the identity of its
#: request file, or ``None`` while that launch has none there (writing it failed). A file
#: that does not match belongs to an older launch and must not be used as this one's. A
#: prefix this process never launched is absent: another window may own its game (#1421).
_stop_requests: dict[str, tuple[int, int, int] | None] = {}


def _prefix_key(prefix: str) -> str:
    return str(Path(prefix).resolve())


def _stop_request_identity(prefix: str) -> tuple[int, int, int] | None:
    """The device, inode and mtime of the saved request, or ``None`` when there is none."""
    try:
        info = stopstate.path(prefix).stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns)


def _forget_stop_request(prefix: str) -> None:
    """Nothing already saved for ``prefix`` speaks for the launch that starts now."""
    _stop_requests[_prefix_key(prefix)] = None
    stopstate.clear(prefix)


def _remember_stop_request(prefix: str) -> None:
    _stop_requests[_prefix_key(prefix)] = _stop_request_identity(prefix)


def _stop_request_is_stale(prefix: str) -> bool:
    """Whether the saved request was not written by this process's running launch."""
    key = _prefix_key(prefix)
    if key not in _stop_requests:
        return False  # another Nadare window launched this game; its file is all we have
    return _stop_requests[key] != _stop_request_identity(prefix)


class LaunchingMixin:
    """Mixin for :class:`WineGameBackend`; see the module text."""

    def _watch_game_process(
        self, done: threading.Event, closed: threading.Event, query=None
    ) -> None:
        """Report a real start; close a known game's launcher after a debounced exit.

        ``query``: the game's own processes when the caller knows them better than
        ``_running_patterns`` (NIKKE's Sail host starts before the game).
        """
        seen = False
        missing_since = None
        query = self._running_patterns() if query is None else query
        while not self.ctx.cancel.cancelled and not done.wait(1.0):
            if self.ctx.cancel.cancelled:
                return
            if scan_pids(query):
                missing_since = None
                if not seen:
                    seen = True
                    self.ctx.emit(Event("game_running", f"{self.display_name} is running"))
            elif seen and self.game_process_names:
                now = time.monotonic()
                if missing_since is None:
                    missing_since = now
                elif now - missing_since >= 3.0:
                    self.info(f"{self.display_name}: the game closed; closing the launcher")
                    self.stop()
                    closed.set()
                    return

    @contextlib.contextmanager
    def _game_process_watch(self, query=None):
        done = threading.Event()
        closed = threading.Event()
        watcher = threading.Thread(
            target=self._watch_game_process,
            args=(done, closed),
            kwargs={} if query is None else {"query": query},
            daemon=True,
        )
        watcher.start()
        try:
            yield closed
        finally:
            done.set()
            watcher.join()

    def _renderer_probe(self) -> graphics.RendererProbe:
        return graphics.RendererProbe(
            lambda: scan_pids(self._patterns()),
            on_result=lambda label: self.info(f"renderer: {label}"),
        )

    #: Games whose existing extra-args hook supplies defaults put them before optional flags.
    launch_extra_args_before_user = False

    def skip_intro_args(self) -> list[str]:
        """Arguments that skip the game's logo videos (default: none)."""
        return []

    def launch_extra_args(self) -> list[str]:
        """Game arguments (normally after the profile's ``launch_args``; default: none)."""
        return []

    def launch_extra_env(self) -> dict[str, str]:
        """Environment added for the game process (default: none)."""
        return {}

    def launch_secrets(self) -> tuple[str, ...]:
        """Values that must never appear in a log line (default: none)."""
        return ()

    def _redacted(self, text: str) -> str:
        for secret in self.launch_secrets():
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def _log_launch_details(self, env: dict[str, str]) -> None:
        """Write the environment and GPU the game got, so a bundled log can explain a bad run."""
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        wanted = set(profile_env) if isinstance(profile_env, dict) else set()
        wanted.update(self.launch_extra_env())
        keys = [
            key
            for key in env
            if (key.startswith(self._LAUNCH_ENV_PREFIXES) or key in wanted)
            and not process._is_secret_name(key)
        ]
        pairs = " ".join(f"{key}={env[key]}" for key in sorted(keys))
        # The whole line goes through _redacted: a value from extra_env may be a launch secret.
        self.info(self._redacted(f"env: {pairs}".rstrip()))
        info = graphics.vulkan_info()
        if not info.available:
            self.info("gpu: Vulkan unavailable")
            return
        parts = [info.device] if info.device else []
        details = []
        if info.driver:
            details.append(info.driver)
        if info.api_version:
            details.append(f"Vulkan {info.api_version}")
        if details:
            parts.append(f"({', '.join(details)})")
        self.info(f"gpu: {' '.join(parts) or 'Vulkan available'}")

    def _direct_argv(self, target: Path) -> list[str]:
        """The program and arguments handed to umu-run for a direct launch."""
        user_args = self._extra_args("launch_args")
        extra_args = self.launch_extra_args()
        return [
            str(target),
            *(extra_args if self.launch_extra_args_before_user else []),
            *self.skip_intro_args(),
            *user_args,
            *(extra_args if not self.launch_extra_args_before_user else []),
        ]

    def _launch_direct(self) -> int:
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._run_vcredist(created=False)
        direct_argv = self._direct_argv(target)
        argv, env = self._prefix_command(
            direct_argv,
            self.launch_extra_env() or None,
            sandboxed=self.secure_mode_enabled(),
        )
        try:
            argv, env = self._secure_wrap(argv, env)
            # After the wrap: Stop asks with a taskkill only when the game really runs
            # unsandboxed, and the Sandbox can be on by default and skipped here.
            self._record_polite_stop(argv, env, len(direct_argv))
            self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
            self.info(self._runtime_note(), **self._runner_facts())
            self.info(self._redacted("exec: " + " ".join(shlex.quote(part) for part in argv)))
            self._log_launch_details(env)
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            self._watch = runtimewatch.RuntimeWatch()  # this launch's output only
            self._launch_started = time.monotonic()
            probe = self._renderer_probe().start()
            try:
                # Held while the game runs, so other Nadare instances (a Flatpak desktop
                # shortcut has its own process namespace) see it and can ask it to stop.
                with self._run_lock(), self._game_process_watch() as closed:
                    self._begin_game_output()
                    try:
                        code = stream(
                            argv,
                            env,
                            on_line=self._on_game_line,
                            cancel=self.ctx.cancel,
                            before_stop=self._ask_game_to_close,
                        )
                    finally:
                        self._end_game_output()
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
        finally:
            self._stop_link_relay()
        return 0 if closed.is_set() else self._checked_launch_exit(code)

    def _explicit_target(self) -> Path | None:
        raw = profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        )
        if not raw:
            return None
        base = self.install_dir() if self.install_dir().is_dir() else self.prefix()
        return path_option(raw, base=base)

    def _search_launcher(self, root: Path) -> Path | None:
        if not root.is_dir():
            return None
        wanted = {name.lower() for name in self.launcher_names}
        visited = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and visited < _SEARCH_MAX_DIRS:
            directory, depth = stack.pop()
            visited += 1
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and entry.name.lower() in wanted:
                    return entry
            if depth < _SEARCH_MAX_DEPTH:
                stack.extend((entry, depth + 1) for entry in entries if entry.is_dir())
        return None

    def _launcher_state(self) -> tuple[str, int | None, int | None] | None:
        """Identity of the launcher on disk, so a failed attempt's file is not counted again."""
        target = self.find_launcher()
        if target is None:
            return None
        try:
            info = target.stat()
        except OSError:
            return str(target), None, None
        return str(target), info.st_mtime_ns, info.st_size

    def find_launcher(self) -> Path | None:
        """Return the launcher exe, or ``None`` when it cannot be found."""
        explicit = self._explicit_target()
        if explicit is not None:
            return explicit if explicit.is_file() else None
        for name in self.launcher_names:
            candidate = self.install_dir() / name
            if candidate.is_file():
                return candidate
        return self._search_launcher(self.prefix() / "drive_c")

    def _target(self) -> Path:
        target = self.find_launcher()
        if target is None:
            raise BackendError(f"{self.display_name} launcher not found under {self.install_dir()}")
        return target

    def _extra_args(self, key: str) -> list[str]:
        raw = profile_setting(self.ctx.profile, key)
        if isinstance(raw, str) and raw.strip():
            return shlex.split(raw)
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw]
        return []

    def _wayland_default(self) -> str:
        """``1`` for native Wayland, ``0`` for XWayland.

        ``display: wayland`` / ``x11`` in the profile wins; ``auto`` (the
        default) follows the backend's ``default_display``. Some newer Proton
        builds hang the Unity client on its first frame under native Wayland
        (NVIDIA), so ``display: x11`` is the escape hatch.
        """
        choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        if choice in ("x11", "xwayland"):
            return "0"
        if choice == "wayland":
            return "1"
        return "1" if self.default_display == "wayland" else "0"

    def _display_in_use(self) -> str:
        """The display Proton will really use, as :meth:`_prefix_command` sets it up (#675).

        On "auto" a ``PROTON_ENABLE_WAYLAND`` the user exported is kept; without a Wayland
        session Proton runs on X11 whatever is asked.
        """
        choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        if choice in ("x11", "xwayland", "wayland"):
            value = self._wayland_default()
        else:
            value = os.environ.get("PROTON_ENABLE_WAYLAND", self._wayland_default())
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        if isinstance(profile_env, dict) and "PROTON_ENABLE_WAYLAND" in profile_env:
            value = str(profile_env["PROTON_ENABLE_WAYLAND"])
        if value != "1":
            return "x11"
        if not os.environ.get("WAYLAND_DISPLAY"):
            return "x11 (no Wayland session)"
        return "wayland"

    def _display_source(self) -> str:
        """Where :meth:`_display_in_use` took the display from, for the launch log (#1299).

        The default is code, not stored data: a release that flips a game's default changes
        old profiles silently, so the log says when the default decided.
        """
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        if isinstance(profile_env, dict) and "PROTON_ENABLE_WAYLAND" in profile_env:
            return "the game's Environment setting"
        choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        if choice in ("x11", "xwayland", "wayland"):
            return "the game's Display setting"
        if "PROTON_ENABLE_WAYLAND" in os.environ:
            return "PROTON_ENABLE_WAYLAND in Nadare's environment"
        return "this game's default"

    def _runner_facts(self) -> dict:
        """The runner this launch uses, for the game page's failure box (#1732)."""
        proton, _kind = self.resolve_runner()
        wine = str(profile_setting(self.ctx.profile, "wine_binary") or "").strip() or None
        return runners.launch_facts(proton, wine=wine)

    def _runtime_note(self) -> str:
        """One log line naming the runner build, display driver and renderer requested."""
        proton, kind = self.resolve_runner()
        runner = runners.runner_version(Path(proton)).split(" ", 1)[-1] if proton else kind
        display = self._display_in_use()
        renderer, _note = graphics.effective_renderer(self.ctx.profile)
        requested = graphics.renderer_choice(self.ctx.profile)
        api = "Vulkan (DXVK)" if renderer == "vulkan" else "OpenGL (WineD3D)"
        source = f", requested {requested}" if requested == "auto" else ""
        return (
            f"runner: {runner or kind}, display: {display}, "
            f"display from: {self._display_source()}, renderer: {api}{source}"
        )

    def _note_prefix_upgrade(self, proton: Path) -> None:
        """Say so when this runner will upgrade the prefix first (#1422): about 40 s of a Play
        that otherwise looks stuck. Once per prefix version change, not per helper command."""
        protoncmd.note_prefix_upgrade(
            self, self.prefix(), proton, name=self.display_name, emit=self.ctx.emit
        )

    def _prefix_command(
        self,
        args: list[str],
        extra_env: dict[str, str] | None = None,
        *,
        sandboxed: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the Wine/``umu-run`` command and environment for ``args``.

        ``sandboxed`` says the command will run in the Sandbox. Inside the Flatpak that decides
        how Proton starts: through umu and the Steam Runtime (as in the AppImage), or, in the
        Sandbox, directly. umu's container needs the session bus, which a Sandboxed game must
        not have: with it a game could ask Flatpak for a process outside the Sandbox.
        """
        prefix = self.prefix()
        env = process.build_env(
            {"pass_env": profile_setting(self.ctx.profile, "pass_env")},
            extra={"WINEPREFIX": str(prefix)},
        )
        proton, kind = self.resolve_runner()
        if kind == "proton":
            env["PROTONPATH"] = str(proton)
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            self._note_prefix_upgrade(proton)
            # A Display choice in the profile beats a PROTON_ENABLE_WAYLAND inherited from the
            # desktop: "x11" is the escape hatch for the NVIDIA first-frame hang and was
            # silently ignored. "auto" still respects a value the user exported.
            choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
            if choice in ("x11", "xwayland", "wayland"):
                env["PROTON_ENABLE_WAYLAND"] = self._wayland_default()
            else:
                env.setdefault("PROTON_ENABLE_WAYLAND", self._wayland_default())
            # The one umu decision every Proton game shares (#1338, #1441, #1449).
            argv = protoncmd.proton_argv(
                args,
                proton=proton,
                prefix=prefix,
                env=env,
                configured=str(profile_setting(self.ctx.profile, "umu_binary", "umu-run")),
                sandboxed=sandboxed,
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
            )
        else:
            # Plain Wine would otherwise ask to download wine-mono / wine-gecko.
            overrides = [p for p in str(env.get("WINEDLLOVERRIDES") or "").split(";") if p.strip()]
            if "mscoree,mshtml=" not in overrides:
                overrides.append("mscoree,mshtml=")
            env["WINEDLLOVERRIDES"] = ";".join(overrides)
            wine = profile_setting(self.ctx.profile, "wine_binary")
            resolved_wine = path_option(wine)
            if (
                resolved_wine is None
                and self._runner_setup_error is not None
                and not (prefix / "drive_c").is_dir()
            ):
                self.info(f"Proton runner setup failed: {self._runner_setup_error}")
                raise BackendError(
                    "The Proton runner could not be downloaded. Check your internet "
                    "connection and press Install again."
                ) from self._runner_setup_error
            if resolved_wine is None and self._wanted_family() is not None:
                # This game runs on Nadare's Proton. Never hand its Proton prefix to whatever
                # plain Wine happens to be on the disk (an old Wine TkG did that, #1338).
                raise BackendError(
                    "The Proton runner for this game is not set up yet. Press Play again to "
                    "download it, or check your internet connection."
                )
            argv = [
                str(resolved_wine) if resolved_wine is not None else str(first_wine()),
                *args,
            ]

        env.update(graphics.renderer_env(self.ctx.profile, kind=kind, environ=env))
        if self.dxvk_config and "DXVK_CONFIG" not in env:
            env["DXVK_CONFIG"] = self.dxvk_config  # profile extra_env still wins
        monitors.apply_env(env, self.ctx)
        shadercache.prepare(
            self.ctx.name,
            env,
            self.info,
            runner=Path(proton) if kind == "proton" and proton else None,
        )
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        process.apply_extra_env(
            env,
            profile_env if isinstance(profile_env, dict) else None,
            reserved=process.LAUNCH_RESERVED_ENV,
        )
        process.apply_extra_env(env, extra_env, reserved=process.LAUNCH_RESERVED_ENV)
        # After the profile's Environment, so a value the user set is kept.
        protoncmd.play_speedups(env, prefix, proton=kind == "proton", info=self.info)
        shadercache.log_caches(
            self.ctx.name, prefix, env, lambda line: self.info(self._redacted(line)),
            sandboxed=sandboxed,
        )
        return argv, process.sanitize_game_env(
            env, preserve=process.launchenv.explicit_names(self.ctx.profile, extra_env)
        )

    def _run_in_prefix(self, argv: list[str]) -> int:
        """Run ``argv`` inside this profile's Wine/Proton prefix."""
        cmd, env = self._prefix_command(list(argv))
        try:
            return stream(cmd, env, on_line=self._on_line, cancel=self.ctx.cancel)
        finally:
            self._wait_for_direct_proton(cmd, env)

    @staticmethod
    def _is_direct_proton(argv: list[str]) -> bool:
        """True for the ``<runner>/proton run`` command used when umu-run is absent."""
        return len(argv) >= 2 and Path(argv[0]).name == "proton" and argv[1] == "run"

    def _wait_for_direct_proton(self, argv: list[str], env: dict[str, str]) -> None:
        """Wait for Wine to finish; ``proton run`` returns before wineboot does.

        umu-run waits by itself, but the direct Proton path inside the Flatpak
        does not: without this the next install step saw Wine's own processes in
        the prefix and refused with "Stop the game before installing".
        """
        if not self._is_direct_proton(argv):
            return
        proton = env.get("PROTONPATH")
        data = env.get("STEAM_COMPAT_DATA_PATH")
        if not proton or not data:
            return
        wineserver = Path(proton) / "files" / "bin" / "wineserver"
        if not wineserver.is_file():
            self.info(f"wineserver not found at {wineserver}; not waiting for Wine to finish")
            return
        wait_env = dict(env)
        # Proton keeps the Wine prefix in <STEAM_COMPAT_DATA_PATH>/pfx.
        wait_env["WINEPREFIX"] = str(Path(data) / "pfx")
        try:
            subprocess.run(
                [str(wineserver), "-w"],
                env=wait_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.info("Wine did not finish within 120 s; carrying on")
        except OSError as exc:
            self.info(f"could not wait for Wine to finish ({exc}); carrying on")

    def _runtime_available(self) -> bool:
        """Whether a Wine/Proton runtime is explicitly configured for direct mode.

        Only an explicitly configured runner or Wine binary counts: a backend
        test that sets neither must never fall back to a real ``wine`` on PATH.
        """
        if self.runner_id():
            return True
        if profile_setting(self.ctx.profile, "wine_binary"):
            return True
        family = self._wanted_family()
        return family is not None and runners.installed_pinned(family) is not None

    def _run_vcredist(self, *, created: bool = False) -> None:
        """Best-effort VC++ runtime step (see :func:`nadare.core.vcredist.ensure`).

        Off unless the class sets ``vcredist_default`` or the profile says ``"vcredist": true``
        (Mongil is the one game here that wants it; Chaos Zero Nightmare has its own backend that
        always installs it). A no-op once the real runtime is present; never aborts the caller.
        """
        if not self.vcredist_default and not as_bool(
            profile_setting(self.ctx.profile, "vcredist"), False
        ):
            return
        vcredist.ensure(
            self.prefix(),
            profile=self.ctx.profile,
            cache_dir=self.cache_dir(),
            run_in_prefix=self._run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    def _ensure_direct_prefix(self) -> None:
        """Create the prefix for a direct install when a runtime is configured."""
        if (self.prefix() / "drive_c").is_dir():
            self._run_vcredist(created=False)
            return
        if self._runtime_available():
            self._ensure_prefix()

    def build_launch(self) -> tuple[list[str], dict[str, str], Path]:
        """Return ``(argv, env, target)`` for launching the client."""
        target = self._target()
        argv, env = self._prefix_command(
            [str(target), *self._extra_args("launch_args")], sandboxed=self.secure_mode_enabled()
        )
        argv, env = self._secure_wrap(argv, env)
        return argv, env, target

    def _run_lock(self) -> contextlib.AbstractContextManager[None]:
        """Inside the Flatpak, let other Nadare instances see this launch and ask it to stop."""
        if not sandbox.in_flatpak():
            return contextlib.nullcontext()
        return runlock.guard(self.prefix(), self._stop_from_elsewhere)

    def _stop_from_elsewhere(self) -> None:
        """Another Nadare instance asked to stop this game: end its processes, then the wait.

        Cancelling alone ends only ``flatpak-spawn``: a Sandboxed game shares Nadare's process
        namespace and keeps running (measured), but its processes are visible here.
        """
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es) for another Nadare window: {pids}")
            self._stop_game_processes(pids)
        self.ctx.cancel.cancel()

    def _stop_game_processes(self, pids: list[int]) -> None:
        """Stop ``pids``; inside the Flatpak, look again until no process of the game is left.

        The game is first asked to close (``taskkill`` without ``/F``) and given a short grace
        to save (#1421 ask 3). A Sandboxed game's Wine helpers can outlive the first sweep
        (measured: a winedevice.exe that ignored SIGTERM and kept the launching Flatpak instance
        alive), and the app's ``wineserver -k`` cannot reach the Sandbox's wineserver.
        """
        pids = self._ask_game_to_close(pids)
        if not pids:
            return
        stop_pids(pids)
        if not sandbox.in_flatpak():
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.5)
            pids = scan_pids(self._patterns())
            if not pids:
                return
            # Still there after SIGTERM: winedevice.exe ignores it (measured), and the app's
            # wineserver -k cannot reach the Sandbox's wineserver.
            stop_pids(pids, signal.SIGKILL)

    def _stop_grace_seconds(self) -> float:
        """How long Stop asks the game to close first; ``0`` skips the polite step."""
        raw = profile_setting(self.ctx.profile, "stop_grace_seconds")
        if raw in (None, ""):
            return STOP_GRACE_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return STOP_GRACE_SECONDS
        if not math.isfinite(value):
            return STOP_GRACE_SECONDS
        return max(0.0, value)

    def _stop_exe_names(self) -> list[str]:
        """Windows image names the polite stop should close, without duplicates."""
        names: list[str] = []
        seen: set[str] = set()

        def add(name: object) -> None:
            text = str(name).strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                names.append(text)

        for query in (self._running_patterns(), self._patterns()):
            for name in getattr(query, "names", ()):
                add(name)
        if self.direct_mode():
            target = self.direct_target()
            if target is not None:
                add(target.name)
        return names

    def _record_polite_stop(
        self, argv: list[str], env: dict[str, str], game_argc: int
    ) -> None:
        """Save what Stop will need, built once from this launch's own command and env.

        A bookkeeping failure must never break Play: without a saved request Stop just
        uses the existing signal path.
        """
        # A new session starts here: an attempt made for an earlier one (the tray launcher
        # stopped before this Play) must not skip this session's polite stop (#1421 review).
        self._polite_stop_attempted = False
        try:
            prefix = self.prefix()
            # The old request is for the previous run; if this one cannot be written, Stop
            # must fall back to signals instead of reusing it (#1421 review).
            _forget_stop_request(prefix)
            # What the launch really did, not what the player asked for: the Sandbox can be
            # on by default and skipped here (no bubblewrap), and the game then runs
            # unwrapped (#1421 review).
            wrapped = self._sandbox_wrapped
            if wrapped is None:
                wrapped = self.secure_mode_enabled()
            if wrapped:
                # A taskkill outside the Sandbox would start a second wineserver on the
                # prefix. Stop asks the game's own windows to close instead.
                stopstate.save(prefix, kind="x11")
                _remember_stop_request(prefix)
                return
            if game_argc <= 0 or game_argc > len(argv):
                stopstate.save(prefix, kind="none")
                _remember_stop_request(prefix)
                return
            head = list(argv[: len(argv) - game_argc])
            names = self._stop_exe_names()
            commands = [[*head, "taskkill", "/IM", name] for name in names]
            stopstate.save(
                prefix,
                kind="taskkill" if commands else "none",
                commands=commands,
                env=env,
            )
            _remember_stop_request(prefix)
        except Exception as exc:  # noqa: BLE001 - a saved request must never break Play
            self.info(
                f"could not save the close request ({exc}); Stop will end the game at once"
            )

    def _run_stop_helper(self, argv: list[str], env: dict[str, str], deadline: float) -> bool:
        """Run one ``taskkill``; False when it failed, hung, or Stop was pressed again."""
        if time.monotonic() >= deadline or self._stop_was_repeated():
            self.info("the close request was interrupted or timed out; ending it now")
            return False
        try:
            proc = process.spawn(
                argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            self.info(f"could not ask the game to close ({exc}); ending it now")
            return False
        try:
            while True:
                if self._stop_was_repeated():
                    self.info("Stop was pressed again; ending the game now")
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.info("asking the game to close took too long; ending it now")
                    return False
                try:
                    code = proc.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if code != 0:
                    self.info(
                        "the game did not accept the close request "
                        f"(taskkill exit {code}); ending it now"
                    )
                    return False
                return True
        finally:
            # A successful wrapper may exit before its taskkill child. Always clean
            # up the session's group, even when the direct child has already exited.
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=1)

    def _stop_was_repeated(self) -> bool:
        return self.ctx.cancel.cancel_count > getattr(self, "_stop_cancel_count", 1)

    def _game_pids(self) -> list[int]:
        """The processes Stop counts as the game: the stop path's query, without helpers."""
        return [pid for pid in scan_pids(self._patterns()) if not _wine_helper(pid)]

    def _ask_game_to_close(self, pids: list[int] | None = None) -> list[int]:
        """Ask the game to close and wait, cheaply, for it to exit.

        Returns the processes that are still running, so the caller can send SIGTERM. An empty
        list means the game exited during the grace and must not be signalled at all. The
        command was saved by the launch; Stop itself never resolves a runner or builds one.
        """
        if pids is None:
            pids = self._game_pids()
        if not pids:
            return []
        grace = self._stop_grace_seconds()
        if grace <= 0 or self._stop_was_repeated():
            return pids
        saved = stopstate.load(self.prefix())
        if saved is None or saved["kind"] == "none":
            self.info("no saved close request for this game; ending it now")
            return pids
        if _stop_request_is_stale(self.prefix()):
            self.info("the saved close request is from an earlier run; ending it now")
            return pids
        # One polite attempt per backend run: after an earlier-instance stop already asked,
        # the cancelled launch must not wait a second grace before it escalates.
        if getattr(self, "_polite_stop_attempted", False):
            return pids
        self._polite_stop_attempted = True
        started = time.monotonic()
        deadline = started + grace
        self.ctx.emit(Event("step", "Asking the game to close…"))
        try:
            if saved["kind"] == "x11":
                return self._ask_sandboxed_to_close(pids, deadline)
            helper_deadline = min(deadline, started + _STOP_HELPER_TIMEOUT)
            # The launch's own environment, rebuilt the way Play built it, plus the
            # difference it saved. Not this process's environment: that would hand the
            # helper the credentials and Python/loader variables a launch filters out.
            helper_env = process.build_env(self.ctx.profile)
            helper_env.update(saved["env"])
            asked = False
            for argv in saved["commands"]:
                # A launcher-only session also knows the game exe, which is not running: one
                # missing name must not throw away the grace another name just earned.
                asked = self._run_stop_helper(argv, helper_env, helper_deadline) or asked
                if self._stop_was_repeated():
                    return pids
            if not asked or time.monotonic() >= helper_deadline:
                return pids
            return self._wait_for_game_exit(deadline)
        finally:
            # The request served this launch. A Stop worker started afterwards (the queued
            # cleanup of the same single Stop) must not wait a second grace for it (#1421).
            stopstate.clear(self.prefix())

    def _ask_sandboxed_to_close(self, pids: list[int], deadline: float) -> list[int]:
        """Ask a Sandboxed game's own windows to close; never start a Wine helper outside.

        No window is not a failure: a Wayland-native game, a session without X11 or a
        missing libX11 just means the existing signals end it at once.
        """
        if not os.environ.get("DISPLAY"):
            self.info("no X display here, so the game was not asked to close; ending it now")
            return pids
        try:
            windows = x11windows.list_windows_owned()
        except Exception:  # noqa: BLE001 - reading windows is optional
            windows = None
        if windows is None:
            self.info(
                "X11 is not available here, so the game was not asked to close; ending it now"
            )
            return pids
        game = [pid for pid in pids if not _wine_helper(pid)]
        owned = launch_window.WindowEvidence(set()).owned_windows(game, dict(windows))
        if not owned:
            self.info(
                "no X11 window was found for the game, so it was not asked to close; "
                "ending it now"
            )
            return pids
        sent = x11windows.request_close(owned)
        if not sent:
            self.info("the X close request could not be sent; ending it now")
            return pids
        return self._wait_for_game_exit(deadline)

    def _wait_for_game_exit(self, deadline: float) -> list[int]:
        """Wait until no game process is left (Wine's helpers do not count), or the grace ends."""
        while True:
            remaining = scan_pids(self._patterns())
            game = [pid for pid in remaining if not _wine_helper(pid)]
            if not game:
                return remaining
            if self._stop_was_repeated():
                return remaining
            now = time.monotonic()
            if now >= deadline:
                self.info("the game did not close in time; ending it now")
                return remaining
            time.sleep(min(_STOP_POLL_SECONDS, max(0.0, deadline - now)))

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        # Fail "not installed" before spending a 320 MB download on a runner.
        if self.direct_mode():
            target = self.direct_target()
            if target is None or not target.is_file():
                raise BackendError(f"game executable not found: {target}")
        elif self.find_launcher() is None:
            raise BackendNotInstalled("the game is not installed")
        self._ensure_default_runner(strict=True)
        if self.direct_mode():
            return self._launch_direct()
        self._run_vcredist(created=False)
        self._watch = runtimewatch.RuntimeWatch()  # this launch's output only
        self._launch_started = time.monotonic()
        try:
            argv, env, target = self.build_launch()
            self._record_polite_stop(
                argv, env, 1 + len(self._extra_args("launch_args"))
            )
            self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
            self.info(self._runtime_note(), **self._runner_facts())
            self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
            self._log_launch_details(env)
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = self._renderer_probe().start()
            try:
                # Held while the game runs, so other Nadare instances (a Flatpak desktop
                # shortcut has its own process namespace) see it and can ask it to stop.
                with self._run_lock(), self._game_process_watch() as closed:
                    self._begin_game_output()
                    try:
                        code = stream(
                            argv,
                            env,
                            on_line=self._on_game_line,
                            cancel=self.ctx.cancel,
                            before_stop=self._ask_game_to_close,
                        )
                    finally:
                        self._end_game_output()
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
        finally:
            self._stop_link_relay()
        return 0 if closed.is_set() else self._checked_launch_exit(code)

    def _checked_launch_exit(self, code: int) -> int:
        """``code``, unless the output showed the runtime failed: then say why.

        A blocked sandbox or a runtime umu could not set up looks like an ordinary exit (umu
        even exits 0), and the page only said "exit code N". Install and update already
        explained these; a launch now does too. After a long session that ended with 0, such a
        line came from a helper the game started, not from the start itself: the hint is shown,
        but the launch is not reported as failed.
        """
        watch = self._watch
        if (
            watch.userns_blocked
            or watch.umu_setup_failed
            or watch.umu_runtime_mismatch
            or watch.no_opengl
        ):
            message = watch.explain(
                f"{self.display_name} could not start (exit code {code})."
            )
            started = getattr(self, "_launch_started", None)
            ran = time.monotonic() - started if started is not None else 0.0
            if code != 0 or ran < self._QUICK_EXIT_SECONDS:
                raise BackendError(message)
            self.ctx.emit(Event("status", message))
        return code

    def stop(self) -> None:
        # This operation itself is the first Stop. Any cancellation, including one
        # delivered before process scanning, is a request to escalate immediately.
        self._stop_cancel_count = 0
        try:
            pids = scan_pids(self._patterns())
            if pids:
                self.info(f"stopping {len(pids)} process(es): {pids}")
                self._stop_game_processes(pids)
            elif self._running_elsewhere():
                stop_elsewhere(self.prefix(), self.info)
                return
            proton: str | None = None
            try:
                resolved, kind = self.resolve_runner()
            except BackendError:
                resolved, kind = None, "wine"
            if kind == "proton" and resolved:
                proton = resolved
            wine = path_option(profile_setting(self.ctx.profile, "wine_binary"))
            stop_runner_wineserver(prefix=self.prefix(), proton=proton, wine_binary=wine)
        finally:
            stopstate.clear(self.prefix())
