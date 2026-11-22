# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The parts of a Proton command every Wine-prefix game shares (#1448, #1449).

The launch command used to be built in three places (the WineGame backends, CZN, the generic
``wine`` kind), and fixes landed in one of them only. Each builder still owns what is its own
(the runner, Display, renderer, Environment), but asks this module for:

* :func:`proton_argv`: the one umu decision. Nadare's own pinned umu first, a system umu-run
  only without it (both inside :func:`umu.resolve`); no bare ``proton run`` on a desktop
  (#1338: it skips the Steam Runtime and every per-game fix); Ubuntu's AppArmor block refused
  before the launch; inside the Flatpak's Sandbox, Proton runs directly (umu needs the session
  bus a Sandboxed game must not have).
* :func:`play_speedups`: the FSR4 setup (no manifest download before RDNA4) and the winebus SDL
  skip without a gamepad, each about 2 s of every Play.

``tests/test_launch_parity_1448.py`` checks every kind that runs a prefix goes through both.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import fsr4, paths, pvlocales, runners, sandbox, umu, userns, winebus
from .backends import BackendError
from .events import Cancel, Emitter, Event


def unavailable_message() -> str:
    """Why umu could not be set up, plus Ubuntu's one-time fix when that is ALSO in the way
    (otherwise the AppArmor block surfaced only after the next attempt; stress u3)."""
    message = umu.unavailable_message()
    if userns.blocks_runtime():
        message = userns.with_hint(message, blocked=True)
    return message


def refuse_if_userns_blocked() -> None:
    """Ask for Ubuntu's one-time AppArmor fix BEFORE the launch (#1338)."""
    if userns.blocks_runtime():
        raise BackendError(userns.with_hint(userns.BLOCKED_BEFORE_LAUNCH, blocked=True))


def resolve_umu(
    configured: str = "umu-run",
    *,
    sandboxed: bool = False,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
) -> str | None:
    """The umu-run to use, or ``None`` when Proton must run directly.

    This is the one umu decision every Proton command shares. A desktop install never
    returns ``None``: no usable umu is an error (#1338). Only inside the Flatpak (its
    Sandbox, or umu unusable there) does Proton run directly.
    """
    flatpak = sandbox.in_flatpak()
    umu_run = None
    if not (flatpak and sandboxed):
        umu_run = umu.resolve(configured, allow_download=True, emit=emit, cancel=cancel)
        if umu_run is None and not flatpak:
            raise BackendError(unavailable_message())
        if umu_run is not None and not flatpak:
            refuse_if_userns_blocked()
    return umu_run


def proton_argv(
    args: list[str],
    *,
    proton: str | Path,
    prefix: Path,
    env: dict[str, str],
    configured: str = "umu-run",
    sandboxed: bool = False,
    emit: Emitter,
    cancel: Cancel | None = None,
) -> list[str]:
    """The argv that runs ``args`` on the Proton runner ``proton``; updates ``env`` in place.

    ``sandboxed`` says the command will run in the Sandbox: inside the Flatpak that means
    ``flatpak-spawn --sandbox``, where umu cannot run, so Proton runs directly.
    """
    umu_run = resolve_umu(configured, sandboxed=sandboxed, emit=emit, cancel=cancel)
    if umu_run:
        owned = False
        if configured in ("", "umu-run") and hasattr(umu, "prepare_runtime"):
            # Nadare's own umu: the runtime goes in its own folder (migration included).
            owned = umu.prepare_runtime(str(umu_run), env, emit)
        if not owned:
            # A system or profile-named umu keeps umu's default location.
            umu.note_first_runtime(emit)
        # Locales the runtime would compile on every launch, kept once (~2 s a Play).
        env.update(pvlocales.prepare(env, paths.cache_dir() / "sandbox-cache"))
        return umu.run_argv(umu_run, args)
    # Only inside the Flatpak (its Sandbox, or umu unavailable there): run the Proton tree
    # directly with the runtime's pinned libraries.
    argv, env_updates = sandbox.proton_direct(Path(proton), prefix, args)
    env.update(env_updates)
    return argv


def note_prefix_upgrade(
    owner: Any, prefix: Path, proton: str | Path, *, name: str, emit: Emitter
) -> None:
    """Say so when this runner will upgrade the prefix first (#1422): about 40 s of a Play
    that otherwise looks stuck. Once per prefix version change (remembered on ``owner``), not
    per helper command."""
    try:
        pending = runners.prefix_upgrade(prefix, Path(proton))
    except Exception:  # noqa: BLE001 - a notice must never stop a launch
        return
    if pending is None or getattr(owner, "_prefix_upgrade_noted", None) == pending:
        return
    owner._prefix_upgrade_noted = pending
    emit(
        Event(
            "step",
            f"{name}: updating the game's Wine prefix for {pending[1]} "
            "(once per runner version; this can take a minute)",
        )
    )


def play_speedups(
    env: dict[str, str], prefix: Path, *, proton: bool, info: Callable[[str], None]
) -> None:
    """The Play-time savings every prefix launch gets; call after the profile's Environment,
    so a value the user set is kept."""
    if proton:
        # DW-Proton's FSR4 upgrade downloads a manifest on every launch; useless before RDNA4.
        env.update(fsr4.upgrade_env(env))
    # No gamepad: skip winebus's SDL scan (~2 s a Play on some kernels).
    note = winebus.prepare(prefix, env)
    if note:
        info(note)
