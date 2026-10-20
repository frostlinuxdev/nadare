# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generic Waydroid (Android) backend.

Ports the ``launch_mode="waydroid"`` row of the stella launcher.  The Android
desktop is always booted with ``waydroid show-full-ui``; a per-app
``waydroid app launch`` is deliberately not used because a bad package id kills
the session instead of degrading.  The package id is read back for status
reporting only.

``/usr/bin/waydroid`` is a ``#!/usr/bin/env python3`` script, so every
Waydroid subprocess runs with a sanitized environment (no
``VIRTUAL_ENV``/``PYTHONHOME``/``PYTHONPATH``) and ``/usr/bin`` first on PATH.

Profile keys (top level or under ``extra``):

``waydroid_package`` (or ``package``)
    Android package id, reported in ``status().data["package_present"]``.
``use_gamescope``
    wrap the launch in gamescope (default false).
``gamescope_backend`` / ``gamescope_args`` / ``gamescope_scaler``
    gamescope wrapper options.
``extra_env``
    extra environment variables for the session.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from ..core import sandbox
from ..core.backends import BackendError, GameBackend, Option, Status
from ..core.events import Event, mask
from .wine import profile_setting, stream

WAYDROID_BINARY = "waydroid"
#: Variables that must never reach a Waydroid subprocess.
_SANITIZE_VARS = (
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PIP_REQUIRE_VIRTUALENV",
)


def _find_on_host(name: str) -> str | None:
    """Locate a host program, asking ``flatpak-spawn`` when sandboxed."""
    if sandbox.in_flatpak():
        return sandbox.host_which(name)
    return shutil.which(name)


def _waydroid_binary() -> Path | None:
    found = _find_on_host(WAYDROID_BINARY)
    return Path(found) if found else None


def _waydroid_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SANITIZE_VARS}
    path = env.get("PATH", "/usr/bin:/bin")
    parts = [p for p in path.split(os.pathsep) if p and p != "/usr/bin"]
    env["PATH"] = os.pathsep.join(["/usr/bin", "/bin", *parts])
    return env


def _waydroid_argv(*args: str, binary: Path | None = None) -> list[str]:
    resolved = binary or _waydroid_binary()
    if resolved is None:
        raise BackendError(f"{WAYDROID_BINARY} is not on PATH (install waydroid)")
    return [str(resolved), *args]


def parse_waydroid_status(text: str) -> dict[str, str]:
    """Parse ``waydroid status`` ``Key: value`` lines into a dict."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def package_in_list(output: str, package: str) -> bool:
    """True when ``package`` is one of the app-list entries in ``output``.

    ``waydroid app list`` prints either bare package ids or ``packageName: <id>``
    lines; the id is compared exactly so ``com.foo`` is not reported present
    merely because ``com.foo.pro`` is installed.
    """
    if not package:
        return False
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        if text == package:
            return True
        _, _, value = text.partition(":")
        if value.strip() == package:
            return True
    return False


class WaydroidBackend(GameBackend):
    kind = "waydroid"

    options = (
        Option("waydroid_package", "Android package", "text", "Package id reported in status."),
        Option("use_gamescope", "Use gamescope", "bool", "Wrap the session in gamescope."),
        Option(
            "gamescope_backend",
            "Gamescope backend",
            "choice",
            "Gamescope window backend.",
            choices=(("SDL", "sdl"), ("Wayland", "wayland"), ("DRM", "drm")),
            advanced=True,
        ),
        Option("gamescope_args", "Gamescope arguments", "args", advanced=True),
        Option("gamescope_scaler", "Gamescope scaler", "text", advanced=True),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
    )

    # ---- profile resolution -----------------------------------------------
    def _package(self) -> str:
        return str(
            profile_setting(self.ctx.profile, "waydroid_package")
            or profile_setting(self.ctx.profile, "package")
            or ""
        ).strip()

    def _binary(self) -> Path:
        binary = _waydroid_binary()
        if binary is None:
            raise BackendError(f"{WAYDROID_BINARY} is not on PATH (install waydroid)")
        return binary

    def build(self) -> tuple[list[str], dict[str, str]]:
        profile = self.ctx.profile
        binary = self._binary()
        env = _waydroid_env()
        extra_env = profile_setting(profile, "extra_env")
        if isinstance(extra_env, dict):
            env.update({str(k): str(v) for k, v in extra_env.items()})

        if not profile_setting(profile, "use_gamescope", False):
            return sandbox.host_argv(_waydroid_argv("show-full-ui", binary=binary), env=env), env

        gamescope = _find_on_host("gamescope")
        if not gamescope:
            raise BackendError("use_gamescope is set but gamescope is not on PATH")
        argv = [gamescope]
        backend = str(profile_setting(profile, "gamescope_backend", "sdl") or "").strip()
        if backend:
            argv += ["--backend", backend]
        raw_args = profile_setting(profile, "gamescope_args", [])
        if isinstance(raw_args, str) and raw_args.strip():
            argv += shlex.split(raw_args)
        elif isinstance(raw_args, (list, tuple)):
            argv += [str(a) for a in raw_args]
        scaler = str(profile_setting(profile, "gamescope_scaler", "") or "").strip()
        if scaler:
            argv += ["-S", scaler]
        inner = " ".join(
            shlex.quote(part) for part in _waydroid_argv("show-full-ui", binary=binary)
        )
        payload = (
            f"export WAYLAND_DISPLAY=${{GAMESCOPE_WAYLAND_DISPLAY:-gamescope-0}}; exec {inner}"
        )
        argv += ["--", "sh", "-c", payload]
        return sandbox.host_argv(argv, env=env), env

    # ---- probes ------------------------------------------------------------
    def _status_map(self) -> dict[str, str]:
        binary = _waydroid_binary()
        if binary is None:
            return {}
        try:
            result = subprocess.run(
                sandbox.host_argv(_waydroid_argv("status", binary=binary), env=_waydroid_env()),
                env=_waydroid_env(),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        return parse_waydroid_status(result.stdout)

    def _package_present(self, package: str) -> bool | None:
        if not package:
            return None
        binary = _waydroid_binary()
        if binary is None:
            return None
        try:
            result = subprocess.run(
                sandbox.host_argv(
                    _waydroid_argv("app", "list", binary=binary), env=_waydroid_env()
                ),
                env=_waydroid_env(),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return package_in_list(result.stdout, package)

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        binary = _waydroid_binary()
        if binary is None:
            return Status(state="not_installed", message="waydroid not found on PATH")
        state = self._status_map()
        session = state.get("Session", "").upper()
        running = session == "RUNNING"
        package = self._package()
        data = {
            "session": session or "unknown",
            "container": state.get("Container", ""),
            "package": package,
        }
        if package:
            data["package_present"] = self._package_present(package)
        return Status(state="running" if running else "installed", running=running, data=data)

    def install(self) -> None:
        raise BackendError(
            "the waydroid backend does not install games; install waydroid and the app separately"
        )

    def update(self) -> None:
        raise BackendError("the waydroid backend does not update apps")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        argv, env = self.build()
        self.ctx.emit(Event("step", "Waydroid: booting Android (show-full-ui)"))
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        return stream(
            argv,
            env,
            on_line=lambda line: self.info(mask(line)),
            cancel=self.ctx.cancel,
            stop_wine=False,
        )

    def stop(self) -> None:
        binary = _waydroid_binary()
        if binary is None:
            raise BackendError("waydroid not found on PATH")
        self.ctx.emit(Event("step", "Waydroid: stopping the session"))
        try:
            subprocess.run(
                sandbox.host_argv(
                    _waydroid_argv("session", "stop", binary=binary), env=_waydroid_env()
                ),
                env=_waydroid_env(),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def verify(self) -> dict:
        binary = _waydroid_binary()
        return {
            "verified": binary is not None,
            "binary": str(binary) if binary else "",
            "session": self._status_map().get("Session", ""),
        }
