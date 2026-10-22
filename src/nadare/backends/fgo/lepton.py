# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Adapter around the ``lepton-fgo`` CLI and its installer/check scripts.

The ``lepton-fgo`` project owns the whole FGO runtime.  Nadare never
reimplements it: this module only locates the tools, builds their command lines
and parses their documented text output in one place.  Every verb maps 1:1 onto
a shell front end:

* ``<prefix>/lepton-x86_64/lepton-fgo`` -- ``status``/``launch``/``stop``/
  ``update``/``check-update``/``config``/``verify``/``uninstall``
* ``<prefix>/lepton-x86_64/lepton-game`` -- the launcher (also used by the
  desktop entry, which passes the package + activity explicitly)
* ``<repo>/install.sh`` -- the installer
* ``<repo>/lepton/check-host.sh`` -- the read-only host readiness report

Documented ``lepton-fgo status <ctx>`` output (see its usage text)::

    context:   fgo
    container: lepton-fgo (running)
    fgo:       2.93.0
    venus:     running
    avahi:     stopped

``fgo`` is only printed while the container is running; ``venus``/``avahi``
report the context's NVIDIA/Venus proxy and mDNS adb helper.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from ...core import paths, sandbox
from ...core.events import Cancel, Cancelled

log = logging.getLogger(__name__)

FGO_PKG = "com.aniplex.fategrandorder.en"

_TOOL_ERROR = (
    "FGO's launcher tool is missing or not executable. Run Install again to repair it."
)
FGO_ACTIVITY = "jp.delightworks.Fgo.player.AndroidPlugin"
DEFAULT_CONTEXT = "fgo"


_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _ident(value: str, what: str) -> str:
    """A context/release name safe to pass as a ``lepton-fgo`` argument.

    A leading ``-`` would be parsed as an option (``--all`` makes ``uninstall``
    remove every context), so names must start with a letter or digit.
    """
    if not _IDENT_RE.fullmatch(value):
        raise LeptonError(f"unsafe {what} name {value!r}: use letters, digits, '.', '_' and '-'")
    return value


class LeptonError(RuntimeError):
    """A lepton-fgo command could not be run or its tool is missing."""


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Stop ``proc`` and its process group (it is started in its own session).

    A group kill reaches the grandchildren an ``install.sh``/``podman`` call
    spawns, which a plain ``terminate`` would leave running.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _host_xdg(name: str, fallback: str, ours: Path) -> Path:
    """The host's XDG base dir: lepton-fgo runs on the host, not in a Flatpak.

    Inside the sandbox ``XDG_*_HOME`` point at ``~/.var/app/<id>/...``; Flatpak
    passes the host's values as ``HOST_XDG_*_HOME`` (unset = the XDG default).
    """
    if not sandbox.in_flatpak():
        return ours
    value = os.environ.get(f"HOST_{name}")
    return Path(value) if value else Path.home() / fallback


def default_prefix() -> Path:
    """``~/.local/share/lepton-nv`` without hard-coding ``~``."""
    return _host_xdg("XDG_DATA_HOME", ".local/share", paths.data_dir().parent) / "lepton-nv"


def default_config_dir() -> Path:
    """``~/.config/lepton-nv`` honoring ``LEPTON_CONFIG_DIR``."""
    configured = os.environ.get("LEPTON_CONFIG_DIR")
    if configured:
        return Path(os.path.expanduser(configured))
    return _host_xdg("XDG_CONFIG_HOME", ".config", paths.config_dir().parent) / "lepton-nv"


def parse_status(text: str) -> dict[str, str | None]:
    """Parse ``lepton-fgo status <ctx>`` into ``{context, container, fgo, ...}``.

    ``container`` keeps the parenthesised state (``lepton-fgo (running)``) so the
    caller can tell a live container from a stopped one; missing lines stay
    ``None`` (``fgo`` is absent while the container is stopped).
    """
    out: dict[str, str | None] = {
        "context": None,
        "container": None,
        "fgo": None,
        "venus": None,
        "avahi": None,
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key in out:
            out[key] = val
    return out


def parse_check_host(text: str) -> dict[str, object]:
    """Parse ``check-host.sh`` output into ``{ok, missing, selinux_enforcing}``."""
    ok_sentence = "OK: all required pieces present." in text
    missing: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("MISSING"):
            for cont in lines[i + 1 :]:
                m = re.match(r"^\s*-\s+(.*\S)\s*$", cont)
                if not m:
                    break
                missing.append(m.group(1))
    enforcing = bool(re.search(r"getenforce:\s*Enforcing", text))
    return {"ok": ok_sentence and not missing, "missing": missing, "selinux_enforcing": enforcing}


def parse_check_update(text: str) -> dict[str, object]:
    """Parse ``lepton-fgo check-update <ctx>`` output."""
    out: dict[str, object] = {"installed": None, "newest": None, "available": None}
    for line in text.splitlines():
        if line.startswith("installed:"):
            out["installed"] = line.split(":", 1)[1].strip()
        elif line.startswith("newest:"):
            out["newest"] = line.split(":", 1)[1].strip()
        elif line.startswith("update available"):
            out["available"] = True
        elif line.startswith("up to date"):
            out["available"] = False
    return out


def parse_config(text: str) -> dict[str, str]:
    """Parse the per-context ``KEY=VALUE`` config (comments/exports ignored)."""
    cfg: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        cfg[key.strip()] = val
    return cfg


class LeptonFgo:
    """Locate and drive one lepton-fgo install (prefix + optional source repo)."""

    def __init__(
        self,
        context: str = DEFAULT_CONTEXT,
        prefix: str | os.PathLike[str] | None = None,
        repo: str | os.PathLike[str] | None = None,
        config_dir: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.context = _ident(str(context or DEFAULT_CONTEXT), "context")
        self.prefix = Path(os.path.expanduser(str(prefix))) if prefix else default_prefix()
        if config_dir:
            self.config_dir = Path(os.path.expanduser(str(config_dir)))
        else:
            self.config_dir = default_config_dir()
        self._repo_arg = (
            os.path.expanduser(str(repo)) if repo else os.environ.get("LEPTON_FGO_REPO")
        )
        self._env_extra = {str(k): str(v) for k, v in (env or {}).items()}

    def set_repo(self, repo: str | os.PathLike[str]) -> None:
        """Point the adapter at a lepton-fgo checkout (e.g. the downloaded installer)."""
        self._repo_arg = os.path.expanduser(str(repo))

    # ---- locating tools ----------------------------------------------------
    def _first(self, *candidates: str | os.PathLike[str] | None) -> Path:
        last: Path | None = None
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            last = path
            if path.exists():
                return path
        return last if last is not None else Path()

    @property
    def bin_dir(self) -> Path:
        return self.prefix / "lepton-x86_64"

    @property
    def lepton_fgo(self) -> Path:
        return self.bin_dir / "lepton-fgo"

    @property
    def lepton_game(self) -> Path:
        return self.bin_dir / "lepton-game"

    @property
    def repo(self) -> Path:
        """The source checkout, preferring one that actually has ``install.sh``.

        Only explicitly trusted locations are consulted: the ``repo`` profile
        key / ``$LEPTON_FGO_REPO`` and the configured install prefix. The process
        working directory and ``~/lepton-fgo`` are deliberately *not* probed --
        an ``install.sh`` found there would be executed without any verification.
        """
        candidates: list[str | os.PathLike[str]] = []
        if self._repo_arg:
            candidates.append(self._repo_arg)
        candidates.append(self.prefix / "installer")
        for candidate in candidates:
            if Path(candidate, "install.sh").is_file():
                return Path(candidate)
        return Path(self._repo_arg) if self._repo_arg else self.prefix / "installer"

    @property
    def install_sh(self) -> Path:
        return self._first(
            self.repo / "install.sh",
            self.prefix / "installer" / "install.sh",
            self.prefix / "install.sh",
        )

    @property
    def check_host_sh(self) -> Path:
        return self._first(
            self.repo / "lepton" / "check-host.sh",
            self.prefix / "installer" / "lepton" / "check-host.sh",
            self.bin_dir / "check-host.sh",
        )

    @property
    def setup_selinux_sh(self) -> Path:
        return self._first(
            self.repo / "lepton" / "setup-host-selinux.sh",
            self.prefix / "installer" / "lepton" / "setup-host-selinux.sh",
            self.bin_dir / "setup-host-selinux.sh",
        )

    @property
    def conf_path(self) -> Path:
        return self.config_dir / f"{self.context}.conf"

    @property
    def installed(self) -> bool:
        """Configured and installed: both the context config and the CLI exist."""
        return self.conf_path.is_file() and self.lepton_fgo.is_file()

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["LEPTON_CONFIG_DIR"] = str(self.config_dir)
        env.update(self._env_extra)
        return env

    def selinux_root_command(self) -> str:
        return f"sudo {self.setup_selinux_sh}"

    # ---- command construction ---------------------------------------------
    def _ctx(self, ctx: str | None) -> str:
        return _ident(ctx, "context") if ctx else self.context
    def cmd_status(self, ctx: str | None = None) -> list[str]:
        return [str(self.lepton_fgo), "status", self._ctx(ctx)]

    def cmd_check_update(self, ctx: str | None = None) -> list[str]:
        return [str(self.lepton_fgo), "check-update", self._ctx(ctx)]

    def cmd_stop(self, ctx: str | None = None) -> list[str]:
        return [str(self.lepton_fgo), "stop", self._ctx(ctx)]

    def cmd_launch(self, ctx: str | None = None) -> list[str]:
        return [str(self.lepton_fgo), "launch", self._ctx(ctx)]

    def cmd_game_launch(self, ctx: str | None = None) -> list[str]:
        """The launcher command the desktop entry installs."""
        return [str(self.lepton_game), self._ctx(ctx), FGO_PKG, FGO_ACTIVITY]

    def cmd_update(self, ctx: str | None = None, force: bool = False) -> list[str]:
        cmd = [str(self.lepton_fgo), "update", self._ctx(ctx)]
        if force:
            cmd.append("--force")
        return cmd

    def cmd_config_get(self, ctx: str | None = None) -> list[str]:
        return [str(self.lepton_fgo), "config", self._ctx(ctx)]

    def cmd_config_set(self, ctx: str | None = None, **kv: object) -> list[str]:
        cmd = [str(self.lepton_fgo), "config", self._ctx(ctx), "set"]
        cmd += [f"{key}={value}" for key, value in kv.items()]
        return cmd

    def cmd_verify(self, ctx: str | None = None, release: str | None = None) -> list[str]:
        cmd = [str(self.lepton_fgo), "verify", self._ctx(ctx)]
        if release:
            cmd += ["--release", _ident(str(release), "release")]
        return cmd

    def cmd_check_host(self) -> list[str]:
        return [str(self.check_host_sh)]

    def cmd_install(
        self,
        mode: str = "strict",
        size: str = "1280x720",
        adopt: bool = False,
        yes: bool = True,
        accept_notice: bool = False,
        data_dir: str | os.PathLike[str] | None = None,
        logs_dir: str | os.PathLike[str] | None = None,
        desktop_dir: str | os.PathLike[str] | None = None,
        cache: str | os.PathLike[str] | None = None,
    ) -> list[str]:
        """Build the ``install.sh`` command Nadare drives.

        ``--yes`` covers prompts; ``--accept-account-notice`` is separate by
        design (the installer refuses to make it implicit), so Nadare passes it
        only when the profile acknowledges the account notice.
        """
        cmd = [
            str(self.install_sh),
            "--context",
            self.context,
            "--prefix",
            str(self.prefix),
            "--config-dir",
            str(self.config_dir),
            "--mode",
            mode,
            "--size",
            size,
        ]
        if adopt:
            cmd.append("--adopt")
        if accept_notice:
            cmd.append("--accept-account-notice")
        if yes:
            cmd.append("--yes")
        if data_dir:
            cmd += ["--data-dir", str(data_dir)]
        if logs_dir:
            cmd += ["--logs-dir", str(logs_dir)]
        if desktop_dir:
            cmd += ["--desktop-dir", str(desktop_dir)]
        if cache:
            cmd += ["--cache", str(cache)]
        return cmd

    def cmd_uninstall(
        self,
        ctx: str | None = None,
        purge: bool = False,
        all_: bool = False,
        purge_cache: bool = False,
        yes: bool = True,
    ) -> list[str]:
        cmd = [str(self.lepton_fgo), "uninstall", self._ctx(ctx)]
        if purge:
            cmd.append("--purge")
        if all_:
            cmd.append("--all")
        if purge_cache:
            cmd.append("--purge-cache")
        if yes:
            cmd.append("--yes")
        return cmd

    # ---- running -----------------------------------------------------------
    def run(
        self,
        cmd: Iterable[str],
        timeout: float = 60,
        cancel: Cancel | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        if cancel is None:
            try:
                return subprocess.run(
                    sandbox.host_argv(argv, env=self.env()),
                    env=self.env(),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                log.debug("lepton-fgo tool not found: %s", argv, exc_info=True)
                raise LeptonError(_TOOL_ERROR) from exc
            except OSError as exc:
                # Not executable, a directory, or another spawn failure: surface the
                # contract's clean LeptonError instead of a raw OSError.
                log.debug("could not run lepton-fgo command %s", argv, exc_info=True)
                raise LeptonError(_TOOL_ERROR) from exc
            except subprocess.TimeoutExpired as exc:
                log.debug("lepton-fgo command timed out after %.0fs: %s", timeout, argv)
                raise LeptonError(
                    "FGO's launcher tool did not respond in time. Close the game and try "
                    "again. Details are in Open logs."
                ) from exc

        # Cancellable variant: the child gets its own session so its whole group
        # (and any grandchildren) can be stopped, while a watcher polls ``cancel``
        # even if the tool prints nothing.
        try:
            proc = subprocess.Popen(
                sandbox.host_argv(argv, env=self.env()),
                env=self.env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            log.debug("could not start lepton-fgo command %s", argv, exc_info=True)
            raise LeptonError(_TOOL_ERROR) from exc

        done = threading.Event()
        threading.Thread(
            target=self._watch_cancel, args=(proc, cancel, done), daemon=True
        ).start()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate(proc)
            log.debug("lepton-fgo command timed out after %.0fs: %s", timeout, argv)
            raise LeptonError(
                "FGO's launcher tool did not respond in time. Close the game and try "
                "again. Details are in Open logs."
            ) from exc
        finally:
            done.set()
            if proc.poll() is None:
                _terminate(proc)
        if cancel.cancelled:
            raise Cancelled()
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    def stream(
        self,
        cmd: Iterable[str],
        on_line: Callable[[str], None] | None = None,
        cancel: Cancel | None = None,
    ) -> int:
        """Run a command, forwarding each merged output line to ``on_line``.

        The child runs in its own session so it and any grandchildren
        (``install.sh`` -> ``podman`` -> ...) can be stopped as one group.
        ``cancel`` is honoured even while the child is silent, and the group is
        stopped on *any* error -- including one raised by ``on_line`` -- so a
        failed log callback never leaks a running installer.
        """
        argv = sandbox.host_argv(list(cmd), env=self.env())
        try:
            proc = subprocess.Popen(
                argv,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            log.debug("could not start lepton-fgo command %s", argv, exc_info=True)
            raise LeptonError(_TOOL_ERROR) from exc

        done = threading.Event()
        if cancel is not None:
            threading.Thread(
                target=self._watch_cancel, args=(proc, cancel, done), daemon=True
            ).start()
        try:
            if proc.stdout is None:
                raise LeptonError("child process has no stdout pipe")
            for raw in proc.stdout:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled()
                if on_line is not None:
                    on_line(raw.rstrip("\n"))
            code = proc.wait()
            if cancel is not None and cancel.cancelled:
                raise Cancelled()
            return code
        finally:
            done.set()
            if proc.poll() is None:
                _terminate(proc)
            if proc.stdout is not None:
                proc.stdout.close()

    @staticmethod
    def _watch_cancel(
        proc: subprocess.Popen[str], cancel: Cancel, done: threading.Event
    ) -> None:
        """Stop the child group as soon as ``cancel`` is set (independent of output)."""
        while not done.wait(0.2):
            if cancel.cancelled:
                _terminate(proc)
                return

    # ---- parsed verbs ------------------------------------------------------
    def status(self, ctx: str | None = None) -> dict[str, str | None]:
        if not self.lepton_fgo.is_file():
            return {
                "context": self._ctx(ctx),
                "container": None,
                "fgo": None,
                "venus": None,
                "avahi": None,
            }
        return parse_status(self.run(self.cmd_status(ctx), timeout=30).stdout)

    def check_host(self, cancel: Cancel | None = None) -> dict[str, object]:
        if not self.check_host_sh.is_file():
            return {
                "ok": False,
                "missing": ["check-host.sh not found"],
                "selinux_enforcing": False,
                "output": "",
            }
        result = self.run(self.cmd_check_host(), timeout=120, cancel=cancel)
        report = parse_check_host(result.stdout + result.stderr)
        report["output"] = result.stdout
        return report

    def check_update(self, ctx: str | None = None) -> dict[str, object]:
        result = self.run(self.cmd_check_update(ctx), timeout=120)
        report = parse_check_update(result.stdout)
        report["output"] = result.stdout
        report["returncode"] = result.returncode
        return report

    def verify(
        self,
        ctx: str | None = None,
        release: str | None = None,
        cancel: Cancel | None = None,
    ) -> dict[str, object]:
        result = self.run(self.cmd_verify(ctx, release), timeout=300, cancel=cancel)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}

    def config(self, ctx: str | None = None) -> dict[str, str]:
        if not self.lepton_fgo.is_file():
            return {}
        return parse_config(self.run(self.cmd_config_get(ctx), timeout=30).stdout)
