# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Building the launch command, starting the game and stopping it.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from ...core import (
    graphics,
    hostlibs,
    monitors,
    process,
    runlock,
    runners,
    runtimewatch,
    sandbox,
    umu,
    userns,
    vcredist,
)
from ...core.backends import (
    BackendError,
)
from ...core.config import as_bool
from ...core.events import Event
from ..wine import (
    first_wine,
    path_option,
    profile_setting,
    scan_pids,
    stop_pids,
    stop_runner_wineserver,
    stream,
)

log = logging.getLogger("nadare.backends.winegame")

#: Bounded discovery: how deep and how many directories to walk in the prefix.
_SEARCH_MAX_DEPTH = 4

_SEARCH_MAX_DIRS = 2000




def _umu_unavailable() -> str:
    """Why umu could not be set up, plus Ubuntu's one-time fix when that is ALSO in the way
    (otherwise the AppArmor block surfaced only after the next attempt; stress u3)."""
    message = umu.unavailable_message()
    if userns.blocks_runtime():
        message = userns.with_hint(message, blocked=True)
    return message


def _refuse_if_userns_blocked() -> None:
    """Ask for Ubuntu's one-time AppArmor fix BEFORE the launch (#1338)."""
    if userns.blocks_runtime():
        raise BackendError(userns.with_hint(userns.BLOCKED_BEFORE_LAUNCH, blocked=True))

class LaunchingMixin:
    """Mixin for :class:`WineGameBackend`; see the module text."""

    def _renderer_probe(self) -> graphics.RendererProbe:
        return graphics.RendererProbe(
            lambda: scan_pids(self._patterns()),
            on_result=lambda label: self.info(f"renderer: {label}"),
        )

    def launch_extra_args(self) -> list[str]:
        """Arguments appended after the profile's ``launch_args`` (default: none)."""
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
        return [str(target), *self._extra_args("launch_args"), *self.launch_extra_args()]

    def _launch_direct(self) -> int:
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._run_vcredist(created=False)
        argv, env = self._prefix_command(
            self._direct_argv(target),
            self.launch_extra_env() or None,
            sandboxed=self.secure_mode_enabled(),
        )
        try:
            argv, env = self._secure_wrap(argv, env)
            self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
            self.info(self._runtime_note())
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
                with self._run_lock():
                    self._begin_game_output()
                    try:
                        code = stream(
                            argv, env, on_line=self._on_game_line, cancel=self.ctx.cancel
                        )
                    finally:
                        self._end_game_output()
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
        finally:
            self._stop_link_relay()
        return self._checked_launch_exit(code)

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

    def _runtime_note(self) -> str:
        """One log line naming the runner build, display driver and renderer requested."""
        proton, kind = self.resolve_runner()
        runner = runners.runner_version(Path(proton)).split(" ", 1)[-1] if proton else kind
        display = self._display_in_use()
        renderer, _note = graphics.effective_renderer(self.ctx.profile)
        requested = graphics.renderer_choice(self.ctx.profile)
        api = "Vulkan (DXVK)" if renderer == "vulkan" else "OpenGL (WineD3D)"
        source = f", requested {requested}" if requested == "auto" else ""
        return f"runner: {runner or kind}, display: {display}, renderer: {api}{source}"

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
        env = process.build_env(None, extra={"WINEPREFIX": str(prefix)})
        proton, kind = self.resolve_runner()
        if kind == "proton":
            configured = str(profile_setting(self.ctx.profile, "umu_binary", "umu-run"))
            flatpak = sandbox.in_flatpak()
            missing = None
            umu_run = None
            if not (flatpak and sandboxed):
                # Every Proton launch outside the Flatpak Sandbox goes through umu (user, #1338):
                # a system umu-run, else Nadare's own (from the AppImage's bundled archive, else
                # downloaded once). A host that merely has the 32-bit libraries no longer runs
                # `proton run`, which skipped every per-game fix and the Steam Runtime.
                umu_run = umu.resolve(
                    configured,
                    allow_download=True,
                    emit=self.ctx.emit,
                    cancel=self.ctx.cancel,
                )
                if umu_run is None and not flatpak:
                    raise BackendError(_umu_unavailable())
                if umu_run is not None and not flatpak:
                    _refuse_if_userns_blocked()
            env["PROTONPATH"] = str(proton)
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            # A Display choice in the profile beats a PROTON_ENABLE_WAYLAND inherited from the
            # desktop: "x11" is the escape hatch for the NVIDIA first-frame hang and was
            # silently ignored. "auto" still respects a value the user exported.
            choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
            if choice in ("x11", "xwayland", "wayland"):
                env["PROTON_ENABLE_WAYLAND"] = self._wayland_default()
            else:
                env.setdefault("PROTON_ENABLE_WAYLAND", self._wayland_default())
            if umu_run:
                umu.note_first_runtime(self.ctx.emit)
                argv = [umu_run, *args]
            else:
                # Only inside the Flatpak (its Sandbox, or umu unavailable there): run the Proton
                # tree directly with the runtime's pinned libraries. A desktop install raised
                # above instead (#1338); the Flatpak path is its own issue.
                if not sandbox.in_flatpak():
                    if missing is None:
                        missing = hostlibs.missing_32bit()
                    if missing:
                        raise BackendError(hostlibs.hint(missing))
                argv, env_updates = sandbox.proton_direct(proton, prefix, args)
                env.update(env_updates)
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
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        process.apply_extra_env(
            env,
            profile_env if isinstance(profile_env, dict) else None,
            reserved=process.LAUNCH_RESERVED_ENV,
        )
        process.apply_extra_env(env, extra_env, reserved=process.LAUNCH_RESERVED_ENV)
        return argv, process.sanitize_game_env(env)

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

        A Sandboxed game's Wine helpers can outlive the first sweep (measured: a winedevice.exe
        that ignored SIGTERM and kept the launching Flatpak instance alive), and the app's
        ``wineserver -k`` cannot reach the Sandbox's wineserver.
        """
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

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        # Fail "not installed" before spending a 320 MB download on a runner.
        if self.direct_mode():
            target = self.direct_target()
            if target is None or not target.is_file():
                raise BackendError(f"game executable not found: {target}")
        elif self.find_launcher() is None:
            raise BackendError("the game is not installed")
        self._ensure_default_runner(strict=True)
        if self.direct_mode():
            return self._launch_direct()
        self._run_vcredist(created=False)
        self._watch = runtimewatch.RuntimeWatch()  # this launch's output only
        self._launch_started = time.monotonic()
        try:
            argv, env, target = self.build_launch()
            self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
            self.info(self._runtime_note())
            self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
            self._log_launch_details(env)
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = self._renderer_probe().start()
            try:
                # Held while the game runs, so other Nadare instances (a Flatpak desktop
                # shortcut has its own process namespace) see it and can ask it to stop.
                with self._run_lock():
                    self._begin_game_output()
                    try:
                        code = stream(
                            argv, env, on_line=self._on_game_line, cancel=self.ctx.cancel
                        )
                    finally:
                        self._end_game_output()
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
        finally:
            self._stop_link_relay()
        return self._checked_launch_exit(code)

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
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            self._stop_game_processes(pids)
        elif self._running_elsewhere():
            self.info("asking the other Nadare window to stop the game")
            runlock.request_stop(self.prefix())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and runlock.running(self.prefix()):
                time.sleep(0.2)
            if runlock.running(self.prefix()):
                self.info("the game did not stop within 20 s")
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
