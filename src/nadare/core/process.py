# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Subprocess helpers, child environments, running-process detection and locks.

Backends are non-interactive: they block and report through an ``Emitter``. This
module gives them one place to spawn children, forward their output as ``Event``
lines, react to cancellation, build a clean Wine/Proton environment, find a
running game by name/prefix/path, and take a per-profile lock so two mutating
operations never run at once.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .events import Cancel, Emitter, Event, emitter, mask

#: How long ``run`` waits after a cancel before escalating to SIGKILL.
GRACE_SECONDS = 5.0
LOCK_DIR_NAME = "locks"


# ---------------------------------------------------------------------------
# child environment
# ---------------------------------------------------------------------------
_BUNDLE_QT_VARS = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


def restore_host_env(environ: dict[str, str] | Any = os.environ) -> None:
    """Undo the PyInstaller bootloader's ``LD_LIBRARY_PATH`` in a frozen build.

    The bootloader points ``LD_LIBRARY_PATH`` at the bundle's private libs and
    keeps the caller's value in ``LD_LIBRARY_PATH_ORIG``. Left alone, every child
    (Proton, Wine, host tools) would load those libs too, e.g. Proton's Python
    loses SSL. This process's loader already has its paths, so restoring the
    host value only affects children. A frozen ``sys.executable -m`` helper gets
    the bundle path again from its own bootloader.
    """
    orig = environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        environ["LD_LIBRARY_PATH"] = orig
    else:
        environ.pop("LD_LIBRARY_PATH", None)
    # The PyQt6 runtime hook points Qt at the bundle; host children (xdg-open
    # and other Qt apps) must not load those plugins.
    for key in _BUNDLE_QT_VARS:
        environ.pop(key, None)


def _xdg_home(value: Path) -> str:
    """``paths`` return ``<XDG>/nadare``; the env var is the parent."""
    return str(value.parent)


#: Python-launcher variables a game/Proton child must not inherit: Proton's own
#: ``proton`` script is ``#!/usr/bin/env python3`` and is sensitive to them.
_PYTHON_ENV_VARS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "VIRTUAL_ENV",
    "PIP_REQUIRE_VIRTUALENV",
)
#: Version-manager shim directories (they resolve ``python3`` through a launcher
#: that may not exist inside a container or after an uninstall).
_SHIM_MARKERS = (
    "/.pyenv/shims",
    "/.asdf/shims",
    "/.rbenv/shims",
    "/mise/shims",
    "/.local/share/shims",
)
_SYSTEM_DIRS = ("/usr/bin", "/bin")


def _system_python_exists() -> bool:
    return any(os.access(os.path.join(d, "python3"), os.X_OK) for d in _SYSTEM_DIRS)


def sanitize_game_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` that is safe to hand to Proton/Wine/umu.

    Drops the Python launcher variables and takes version-manager shims and an
    active virtualenv's ``bin`` off ``PATH`` (a stale ``pyenv`` shim ahead of
    ``/usr/bin`` makes ``proton``'s ``env python3`` exit 127). Nothing is
    stripped when there is no system ``python3`` to fall back to, so a machine
    whose only Python is a shim keeps working.
    """
    out = dict(env)
    if not _system_python_exists():
        return out
    venv = out.get("VIRTUAL_ENV", "").rstrip("/")
    for name in _PYTHON_ENV_VARS:
        out.pop(name, None)
    kept: list[str] = []
    for entry in out.get("PATH", "").split(":"):
        norm = entry.rstrip("/")
        if not norm:
            continue
        if any(marker in norm + "/" or norm.endswith(marker) for marker in _SHIM_MARKERS):
            continue
        if venv and (norm == venv + "/bin" or norm.startswith(venv + "/")):
            continue
        kept.append(entry)
    for system in _SYSTEM_DIRS:
        if system not in kept:
            kept.append(system)
    out["PATH"] = ":".join(kept)
    return out


def build_env(
    profile: Mapping[str, Any] | None = None,
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment.

    The caller's environment is inherited unchanged (so ``DISPLAY``,
    ``WAYLAND_DISPLAY``, ``XAUTHORITY``, ``DBUS_SESSION_BUS_ADDRESS`` and friends
    survive), then Nadare's XDG roots are pinned so children never write to a
    hard-coded ``~/.local``. Profile ``prefix``/``runner`` become
    ``WINEPREFIX``/``PROTONPATH`` when set. ``extra`` is merged last, so a caller
    can override anything.
    """
    env: dict[str, str] = dict(os.environ if base is None else base)

    env["XDG_DATA_HOME"] = _xdg_home(paths.data_dir())
    env["XDG_CONFIG_HOME"] = _xdg_home(paths.config_dir())
    env["XDG_STATE_HOME"] = _xdg_home(paths.state_dir())
    env["XDG_CACHE_HOME"] = _xdg_home(paths.cache_dir())
    if "XDG_RUNTIME_DIR" not in env and hasattr(os, "getuid"):
        runtime = Path(f"/run/user/{os.getuid()}")
        try:
            if runtime.is_dir() and runtime.stat().st_uid == os.getuid():
                env["XDG_RUNTIME_DIR"] = str(runtime)
        except OSError:
            pass

    if profile:
        prefix = str(profile.get("prefix") or "").strip()
        if prefix:
            env["WINEPREFIX"] = str(Path(prefix).expanduser())
        runner = str(profile.get("runner") or "").strip()
        if runner:
            env["PROTONPATH"] = str(Path(runner).expanduser())
        env.setdefault("WINEDEBUG", env.get("WINEDEBUG", "-all"))

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# process detection
# ---------------------------------------------------------------------------
@dataclass
class ProcessInfo:
    pid: int
    name: str
    cmdline: list[str] = field(default_factory=list)
    exe: str | None = None

    @property
    def joined(self) -> str:
        return " ".join(self.cmdline)


def _read_cmdline(pid: int) -> tuple[list[str], str | None, str]:
    base = Path("/proc") / str(pid)
    try:
        raw = (base / "cmdline").read_bytes()
    except OSError:
        return [], None, ""
    parts = [p for p in raw.split(b"\x00") if p]
    cmdline = [p.decode("utf-8", "replace") for p in parts]
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = None
    try:
        name = (base / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        # Fall back to the first token of the command line.
        name = Path(cmdline[0]).name if cmdline else ""
    return cmdline, exe, name


def iter_processes() -> Iterator[ProcessInfo]:
    """Yield the current process table from ``/proc`` (Linux only)."""
    try:
        entries = sorted(p.name for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return
    for entry in entries:
        try:
            pid = int(entry)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        cmdline, exe, name = _read_cmdline(pid)
        if not cmdline and not exe:
            continue
        yield ProcessInfo(pid=pid, name=name or Path(cmdline[0]).name, cmdline=cmdline, exe=exe)


def _under_prefix(value: str, prefix: str) -> bool:
    """True when ``value`` is ``prefix`` or a path *below* it.

    A bare ``startswith`` lets ``/a/b`` match ``/a/bc``; require exact equality
    or a path separator at the boundary.
    """
    if value == prefix:
        return True
    if not value.startswith(prefix):
        return False
    return value[len(prefix) :].startswith(os.sep)


def find_processes(
    *,
    prefix: str | os.PathLike[str] | None = None,
    name: str | None = None,
    cmdline_contains: str | None = None,
) -> list[ProcessInfo]:
    """Return processes matching any supplied criterion.

    ``prefix`` matches the executable path or any argument that is the prefix or
    sits below it (e.g. a game install directory); ``name`` matches the basename
    of the executable or the kernel ``comm``; ``cmdline_contains`` is a
    case-insensitive substring of the full command line.
    """
    prefix_s = str(Path(prefix).expanduser()) if prefix else None
    needle = cmdline_contains.lower() if cmdline_contains else None
    out: list[ProcessInfo] = []
    for proc in iter_processes():
        if name:
            first = Path(proc.cmdline[0]).name if proc.cmdline else ""
            if proc.name != name and first != name:
                continue
        if prefix_s:
            candidates = [proc.exe or ""] + proc.cmdline
            if not any(_under_prefix(c, prefix_s) for c in candidates):
                continue
        if needle and needle not in proc.joined.lower():
            continue
        out.append(proc)
    return out


def is_running(
    *,
    prefix: str | os.PathLike[str] | None = None,
    name: str | None = None,
    cmdline_contains: str | None = None,
) -> bool:
    return bool(find_processes(prefix=prefix, name=name, cmdline_contains=cmdline_contains))


def _pid_starttime(pid: int) -> str | None:
    """Start time (clock ticks since boot) of ``pid``, or ``None`` if unreadable.

    Two different processes never share a start time, so a changed value across
    the grace window means the PID was recycled onto an unrelated process.
    """
    try:
        data = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        # Field 22; the comm (field 2) may contain spaces/parens, so split on
        # the last ')' and index from there (field 3 is index 0).
        return data.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def terminate(pid: int, *, grace: float = GRACE_SECONDS) -> bool:
    """SIGTERM a pid, escalating to SIGKILL. Returns True if it had to be killed."""
    started = _pid_starttime(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.1)
    # Revalidate identity before escalating: if the process exited and the PID
    # was recycled, SIGKILL must not land on a bystander.
    if started is not None:
        now = _pid_starttime(pid)
        if now is None or now != started:
            return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------------------
# spawning / running
# ---------------------------------------------------------------------------
def spawn(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.STDOUT,
    start_new_session: bool = True,
    **popen_kwargs: Any,
) -> subprocess.Popen[str]:
    """Start a child process with sane defaults (own session, no stdin).

    ``start_new_session=True`` keeps Ctrl-C on the terminal from racing the
    launcher's own handler and lets us signal the whole process group.
    """
    cmd = [str(a) for a in args]
    return subprocess.Popen(  # noqa: S603 - args are backend-controlled
        cmd,
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
        start_new_session=start_new_session,
        **popen_kwargs,
    )


def run(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    capture: bool = False,
    check: bool = False,
    stream: bool = True,
    forward_stderr: bool = True,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a child to completion, forwarding output and honouring cancellation.

    ``emit`` receives one ``log`` event per output line (unless ``stream`` is
    False), a ``status`` event for the spawn, and an ``error`` event on failure.
    ``capture`` collects the same lines on ``CompletedProcess.stdout``. On
    cancel the child's process group is told to stop and ``Cancelled`` is raised.
    """
    say = emitter(emit)
    say(Event("status", mask(f"$ {' '.join(str(a) for a in args)}"), data={"stage": "spawn"}))

    stderr_mode = subprocess.STDOUT if forward_stderr else subprocess.PIPE
    proc = spawn(args, env=env, cwd=cwd, stderr=stderr_mode, **popen_kwargs)

    lines: list[str] = []
    line_q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line_q.put(line.rstrip("\n"))
        finally:
            line_q.put(None)

    reader = threading.Thread(target=_reader, name="nadare-run-reader", daemon=True)
    reader.start()

    def _drain() -> None:
        while True:
            try:
                item = line_q.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            lines.append(item)
            if stream:
                say(Event("log", item))

    cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel.cancelled:
            cancelled = True
            _signal_group(proc)
            break
        try:
            item = line_q.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is not None:
            lines.append(item)
            if stream:
                say(Event("log", item))

    try:
        proc.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(proc, force=True)
        proc.wait()
    reader.join(timeout=1.0)
    _drain()

    cp = subprocess.CompletedProcess(
        list(str(a) for a in args), proc.returncode, "\n".join(lines) if capture else None, None
    )
    if cancelled:
        raise _cancelled_exc()
    if check and cp.returncode != 0:
        say(
            Event(
                "error",
                f"command failed ({cp.returncode}): {cp.args[0]}",
                data={"code": cp.returncode},
            )
        )
        raise subprocess.CalledProcessError(cp.returncode, cp.args, cp.stdout)
    return cp


def _signal_group(proc: subprocess.Popen[Any], *, force: bool = False) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill() if force else proc.terminate()
        except OSError:
            pass


def _cancelled_exc() -> BaseException:
    from .events import Cancelled

    return Cancelled("operation cancelled")


def which(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    return shutil.which(name, path=(env or os.environ).get("PATH"))


# ---------------------------------------------------------------------------
# per-profile lock
# ---------------------------------------------------------------------------
class ProfileLock:
    """An advisory ``flock`` guarding mutating operations on one profile.

    Usage::

        with ProfileLock("my-game"):
            ...

    ``acquire`` is non-blocking by default; pass ``wait`` seconds to poll. A
    lost lock raises :class:`ProfileLockError`.
    """

    def __init__(self, profile: str, *, wait: float = 0.0, poll: float = 0.2) -> None:
        self.profile = profile
        self.wait = wait
        self.poll = poll
        self.path = paths.state_dir() / LOCK_DIR_NAME / f"{_safe(profile)}.lock"
        self._fd: int | None = None

    def acquire(self) -> ProfileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(self.wait, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise ProfileLockError(f"profile {self.profile!r} is busy") from None
                time.sleep(self.poll)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ProfileLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class ProfileLockError(RuntimeError):
    """Another Nadare operation already holds the profile lock."""


def _safe(name: str) -> str:
    """Map a profile name to a lock filename: readable slug + name digest.

    A plain character collapse is many-to-one (``a b`` and ``a_b`` would share a
    lock file), so a digest of the original name is appended to keep distinct
    profiles on distinct locks.
    """
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in name]
    slug = "".join(keep).strip("._") or "profile"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:100]}-{digest}"
