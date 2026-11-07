# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Sandbox: run a game with the real home folder hidden (bubblewrap).

A Windows process under Wine can read the whole Linux home through ``Z:`` or the
``\\\\?\\unix\\`` path, and drive letters cannot be taken away (measured, #804). What does
work is a mount namespace: the home folder is replaced by an empty tmpfs and only the folders
the game needs are bound back. This module builds that command; it never runs anything.

Not in ``core/sandbox.py``: that one is about running inside Flatpak.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import hostlibs, linkrelay, paths, prefixguard, process, userns

BINARY = "bwrap"

#: Other drives and removable media: hidden so a game cannot read them.
_HIDDEN_MOUNTS = ("/mnt", "/media", "/run/media")

#: What of the session runtime folder a game still gets: the display (wayland-*, gamescope-*)
#: and the sound.
#: The session bus, the keyring and the ssh/gpg agents are deliberately not here.
_RUNTIME_KEEP = ("pulse", "pipewire-0", "pipewire-0-manager")

#: Variables that point at sockets Sandbox hides; left set, the game's own container
#: (pressure-vessel) would try to bind them and stop.
_HIDDEN_ENV = ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO")

#: Where a Sandboxed game's shader caches go: its own folder inside Nadare's cache folder, so
#: they survive between launches without showing the game the rest of ``~/.cache``.
_CACHE_FOLDER = "sandbox-cache"


class BroadBindError(ValueError):
    """A game folder is so broad that binding it would give the hidden home folder back."""


def runtime_copies() -> list[str]:
    """The ``srt-bwrap`` copies in the Steam Runtime that umu downloads for Proton, newest first.

    Every Proton game Nadare runs already has one on disk, so Sandbox needs nothing extra.
    """
    return [path for path in userns._srt_bwrap_paths() if os.access(path, os.X_OK)]


def available() -> bool:
    """True when some ``bwrap`` exists (the runtime's copy or the system's). Never raises."""
    return bool(runtime_copies()) or shutil.which(BINARY) is not None


#: Where Nadare records which ``srt-bwrap`` copies it trusts (path and sha256). Outside every
#: folder a Sandboxed game can write to.
PINS_FILE = "sandbox-runtime.json"


def pins_path() -> Path:
    return paths.state_dir() / PINS_FILE


def _sha256(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _read_pins() -> dict[str, str] | None:
    """The recorded copies, ``None`` when nothing was recorded yet, ``{}`` when it is damaged."""
    try:
        text = pins_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _record_runtime_copies() -> dict[str, str]:
    pins = {copy: digest for copy in runtime_copies() if (digest := _sha256(copy))}
    target = pins_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(pins, indent=1), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        pass  # nothing recorded: the next start asks again, and no copy is run without a record
    return pins


def trusted_runtime_copies() -> list[str]:
    """The runtime ``srt-bwrap`` copies Nadare recorded and that still match, newest first.

    Nadare runs this file outside the Sandbox, and the folder it sits in is writable by the
    game, so a game could plant a newer copy for the next launch (#871). Copies present the
    first time the Sandbox is used are recorded then (nothing Sandboxed has run before that);
    a copy that appears later, or whose content changed, is never run.
    """
    pins = _read_pins()
    if pins is None:
        pins = _record_runtime_copies()
    return [copy for copy in runtime_copies() if pins.get(copy) and _sha256(copy) == pins[copy]]


def recorded_runtime_copies() -> list[str]:
    """Like :func:`trusted_runtime_copies`, but never records anything.

    For callers that only want to look (``nadare doctor``): the first call to
    ``trusted_runtime_copies`` pins whatever is on disk then, and that must be the Sandbox's
    first start, not a diagnostic that a game could have been waiting for (#965).
    """
    pins = _read_pins()
    if not pins:
        return []
    return [copy for copy in runtime_copies() if pins.get(copy) and _sha256(copy) == pins[copy]]


@dataclass(frozen=True)
class Located:
    """The bubblewrap Sandbox will use, or why there is none."""

    binary: str | None  # what to run; None when nothing works
    source: str = ""  # "runtime" or "system"
    found: bool = False  # some copy exists (so a failure is "blocked", not "not installed")
    why: str = ""  # the first reason a copy could not start


def locate() -> Located:
    """Pick a bubblewrap that really starts here: the runtime's own copy first, then the system's.

    The runtime copy is tried first because it is already on disk, is the one the AppArmor profile
    Nadare installs on Ubuntu allows, and is the same one Proton's container uses. Only copies
    Nadare recorded are run (see :func:`trusted_runtime_copies`).
    """
    trusted = trusted_runtime_copies()
    candidates = [(path, "runtime") for path in trusted]
    if shutil.which(BINARY) is not None:
        candidates.append((BINARY, "system"))
    first_why = ""
    unrecorded = [copy for copy in runtime_copies() if copy not in trusted]
    for binary, source in candidates:
        started, why = works(binary)
        if started:
            return Located(binary, source, True)
        first_why = first_why or why
    if not first_why and unrecorded:
        first_why = (
            "the game runtime has a bubblewrap Nadare did not record, so it is not run "
            "(a game could have put it there). If you trust this runtime, delete "
            f"{pins_path()} and start again"
        )
    return Located(None, "", bool(candidates) or bool(unrecorded), first_why)


def install_hint(family: str | None) -> str:
    """The command that installs bubblewrap on this kind of distro (a plain hint when unknown)."""
    template = hostlibs._INSTALL.get(family or "")
    if template is None:
        return "install the 'bubblewrap' package with your package manager"
    return template.replace("sudo dpkg --add-architecture i386 && sudo apt update && ", "").format(
        pkgs="bubblewrap"
    )


def works(binary: str = BINARY) -> tuple[bool, str]:
    """Whether ``binary`` can really start here, and the first line of why not.

    Ubuntu 24.04 and newer block unprivileged user namespaces for programs without an AppArmor
    profile; the profile Nadare installs covers the runtime's own copy.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [binary, "--die-with-parent", "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ""
    lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
    return False, lines[0] if lines else f"bwrap exited with {done.returncode}"


def _existing(path: str | Path | None) -> Path | None:
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.exists() else None


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def wrap(
    argv: list[str],
    env: Mapping[str, str],
    *,
    prefix: Path,
    install_dir: Path | None = None,
    runner_dir: Path | None = None,
    home: Path | None = None,
    link_pipe: Path | None = None,
    bwrap: str = BINARY,
) -> tuple[list[str], dict[str, str]]:
    """Return ``([bwrap, ...flags, "--", *argv], env)`` with the real home hidden.

    The home is resolved first (``readlink -f``): on Fedora Atomic ``/home`` is a symlink and
    bubblewrap cannot mount over it, but it can over what it points to, which hides the home
    through both paths. Paths that do not exist are skipped rather than failing the launch.
    """
    real_home = Path(home or Path.home()).resolve()
    flags: list[str] = ["--die-with-parent", "--dev-bind", "/", "/", "--tmpfs", str(real_home)]
    hidden: list[str] = []
    for mount in _HIDDEN_MOUNTS:
        # bwrap cannot mount on a symlink (/mnt -> var/mnt on Bazzite): use the target.
        target = str(Path(mount).resolve())
        if Path(target).is_dir() and target not in hidden:
            hidden.append(target)
            flags += ["--tmpfs", target]

    runtime = _existing(env.get("XDG_RUNTIME_DIR"))
    if runtime is not None:
        flags += ["--tmpfs", str(runtime)]
        # Wayland and gamescope sockets (gamescope's Vulkan layer needs its own: without it a game
        # run inside gamescope never gets a swapchain and renders nothing).
        keep = [
            p
            for pattern in ("wayland-*", "gamescope-*")
            for p in runtime.glob(pattern)
            if not p.name.endswith((".lock", "-lock"))
        ]
        keep += [runtime / name for name in _RUNTIME_KEEP if (runtime / name).exists()]
        for path in sorted(set(keep)):
            flags += ["--bind", str(path), str(path)]

    bound: list[Path] = []

    def bind(path: Path | None, *, read_only: bool = False) -> None:
        if path is None or any(_inside(path, done) for done in bound):
            return
        bound.append(path)
        flags.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])

    for folder in (prefix, install_dir):
        # Binding the home folder, a folder holding it, or a whole drive after hiding it would
        # put it straight back: the Sandbox would look on and protect nothing (#850).
        reason = prefixguard.broad_reason(folder, real_home) if folder is not None else ""
        if reason:
            raise BroadBindError(f"{folder} is {reason}")
    resolved_prefix = _existing(prefix)
    bind(resolved_prefix)
    bind(_existing(install_dir))
    bind(_existing(runner_dir), read_only=True)
    # Nadare's config, data and state folders are not bound: the game gets its own prefix and
    # install dir (above) and nothing else of Nadare's. The umu runtime is writable because umu
    # keeps the runtime and its lock files there, but the runtime's bubblewrap, which Nadare runs
    # outside the Sandbox, is bound read-only on top so a game cannot replace it.
    bind(_existing(real_home / ".local/share/umu"))
    for copy in runtime_copies():
        copy_path = _existing(copy)
        if copy_path is not None:
            flags.extend(["--ro-bind", str(copy_path), str(copy_path)])
    cache_root = env.get("XDG_CACHE_HOME")
    cache = Path(cache_root) / paths.APP / _CACHE_FOLDER if cache_root else None
    if cache is not None:
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache = None
    bind(_existing(cache))
    # The program being started may itself live in the home (umu-run or a wine build in
    # ~/.local/bin): without this it would vanish with the home and bwrap could not start it.
    program = _existing(argv[0]) if argv and Path(argv[0]).is_absolute() else None
    if program is not None and program.is_file() and _inside(program, real_home):
        bind(program, read_only=True)

    # Where the Steam Runtime looks for the Steam client's pipe: Nadare's link relay stands in
    # for it, so "Sign in with Google" can still reach the browser (see linkrelay.py).
    if link_pipe is not None:
        flags.extend(["--bind", str(link_pipe), str(real_home / linkrelay.PIPE_RELATIVE)])

    # X11 needs its authority file, which usually sits in the home or the runtime folder.
    bind(_existing(env.get("XAUTHORITY")), read_only=True)

    wrapped_env = {k: v for k, v in env.items() if k not in _HIDDEN_ENV}
    if cache is not None:
        wrapped_env["XDG_CACHE_HOME"] = str(cache)
    return [bwrap, *flags, "--", *argv], wrapped_env


__all__ = [
    "BINARY",
    "Located",
    "available",
    "install_hint",
    "locate",
    "runtime_copies",
    "wrap",
    "works",
]
