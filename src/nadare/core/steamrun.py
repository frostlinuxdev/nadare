# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Steam Set up actions: what they run, in which order, and what they refuse.

This is the executing half of ``steamsetup`` and ``steamfix``.  Every side
effect is injected -- a ``run(argv) -> int`` process runner, the Steam-running
and wait-for-close probes, the prefix functions -- so the actions can be tested
without a real Steam, Wine, display or network.  Nothing here builds a command
by string concatenation or uses a shell.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import steamfix, steamtools, steamwayland
from .backends import BackendError
from .events import Cancelled
from .steamtools import CompatEntry

#: How long the client gets to flush ``config.vdf`` and exit after ``-shutdown``.
STEAM_CLOSE_TIMEOUT = 60.0

#: One runner change at a time in this process: two would read the same ``config.vdf`` and the
#: second write would silently undo the first.
_RUNNER_LOCK = threading.Lock()


@dataclass(frozen=True)
class ActionResult:
    """The plain outcome of one Set up action."""

    ok: bool
    line: str
    changed: bool = False
    backup: Path | None = None
    previous: CompatEntry | None = None


def _numeric_id(app_id: str | int) -> str:
    """The app id as text; non-digits are refused before they reach a path or Steam."""
    key = str(app_id)
    if not key or not key.isascii() or not key.isdigit():
        raise BackendError(f"Steam app id must be numeric, got {key!r}")
    return key


# ---- steam:// requests -----------------------------------------------------


def request_steam_install(
    app_id: str | int,
    *,
    run: Callable[[list[str]], int],
    binary: str | None = None,
) -> ActionResult:
    """Ask Steam to install one app; a bad id never reaches the client."""
    key = _numeric_id(app_id)
    argv = [*steamtools.steam_command(binary), f"steam://install/{key}"]
    code = run(argv)
    if code == _NOT_FOUND:
        return ActionResult(False, _NO_STEAM_COMMAND)
    if code != 0:
        return ActionResult(False, f"Steam could not start the install (exit code {code}).")
    return ActionResult(True, f"Steam is installing app {key}.")


#: The shell's "command not found", which ``steamglue`` also returns when the client is missing.
_NOT_FOUND = 127
_NO_STEAM_COMMAND = (
    "Nadare could not find Steam's start command. Start Steam yourself and try again, or set "
    "the Steam command in this game's Settings."
)


def unlock_h264(*, run: Callable[[list[str]], int], binary: str | None = None) -> ActionResult:
    """Run the client's own global H.264 unlock request once."""
    argv = steamtools.unlockh264_command(binary)
    code = run(argv)
    if code == _NOT_FOUND:
        return ActionResult(False, _NO_STEAM_COMMAND)
    if code != 0:
        return ActionResult(False, f"Steam's H.264 request failed (exit code {code}).")
    return ActionResult(True, "Steam's H.264 video support was requested.")


# ---- the runner change -----------------------------------------------------


def _change_runner(
    app_id: str | int,
    *,
    config_path: Path,
    run: Callable[[list[str]], int],
    steam_running: Callable[[], bool],
    wait_gone: Callable[[float], bool],
    binary: str | None,
    change: Callable[[], Any],
    describe: Callable[[Any, str], str] | None = None,
    undo_of: Callable[[Any], object] | None = None,
    game_running: Callable[[], bool] | None = None,
) -> ActionResult:
    """Close Steam if needed, apply ``change``, and start Steam again if it was up.

    A client that does not close is left alone: nothing is written and it is not
    started on top of itself.  Once it did close, it is started again in every
    case, including a refused or failed edit, so the user is not left without it; a
    restart that fails is said so in the result. A game that is still running keeps the
    settings it started with, so nothing is closed or written while it is.
    """
    if not _RUNNER_LOCK.acquire(blocking=False):
        return ActionResult(
            False, "Another Steam change is already running. Wait for it to finish."
        )
    try:
        running = game_running or (lambda: steamfix.game_running(app_id))
        if running():
            return ActionResult(
                False,
                "The game is still running, so it would keep its old settings. "
                "Close the game, then try again; nothing was changed.",
            )
        was_running = bool(steam_running())
        if was_running:
            run(steamtools.shutdown_command(binary))
            if not wait_gone(STEAM_CLOSE_TIMEOUT):
                return ActionResult(
                    False,
                    "Steam did not close. Close Steam and try again; nothing was changed.",
                )

        def restart() -> str:
            if not was_running:
                return ""
            code = run(steamtools.start_command(binary))
            if code == 0:
                return ""
            return f" Nadare could not start Steam again (exit code {code}); start it yourself."

        try:
            edit = change()
        except Exception as exc:  # noqa: BLE001 - Steam must come back whatever went wrong
            return ActionResult(False, f"The Steam setting was not changed: {exc}{restart()}")
        note = restart()
        if describe is not None:
            line = describe(edit, note)
        elif edit.tool:
            line = f"Steam now uses {edit.tool} for app {app_id}.{note}"
        else:  # an undo of a game that had no setting of its own
            line = f"Steam's default runner is back for app {app_id}.{note}"
        previous = undo_of(edit) if undo_of is not None else edit.previous
        return ActionResult(True, line, edit.changed, edit.backup, previous)
    finally:
        _RUNNER_LOCK.release()


def set_runner(
    app_id: str | int,
    tool: str,
    *,
    config_path: Path,
    run: Callable[[list[str]], int],
    steam_running: Callable[[], bool],
    wait_gone: Callable[[float], bool],
    binary: str | None = None,
    game_running: Callable[[], bool] | None = None,
    apply: Callable[..., steamtools.CompatEdit] = steamtools.apply_compat_tool,
) -> ActionResult:
    """Set one game's compatibility tool (Steam is closed for the edit)."""
    return _change_runner(
        app_id,
        config_path=config_path,
        run=run,
        steam_running=steam_running,
        wait_gone=wait_gone,
        binary=binary,
        game_running=game_running,
        change=lambda: apply(config_path, app_id, tool, steam_running_fn=steam_running),
    )


def restore_runner(
    app_id: str | int,
    previous: CompatEntry | None,
    *,
    config_path: Path,
    run: Callable[[list[str]], int],
    steam_running: Callable[[], bool],
    wait_gone: Callable[[float], bool],
    binary: str | None = None,
    game_running: Callable[[], bool] | None = None,
) -> ActionResult:
    """Put a game's earlier compatibility entry back exactly."""
    return _change_runner(
        app_id,
        config_path=config_path,
        run=run,
        steam_running=steam_running,
        wait_gone=wait_gone,
        binary=binary,
        game_running=game_running,
        change=lambda: steamtools.restore_compat_tool(
            config_path, app_id, previous, steam_running_fn=steam_running
        ),
    )


def set_wayland(
    app_id: str | int,
    *,
    localconfig_path: Path,
    run: Callable[[list[str]], int],
    steam_running: Callable[[], bool],
    wait_gone: Callable[[float], bool],
    binary: str | None = None,
    game_running: Callable[[], bool] | None = None,
    apply: Callable[..., steamtools.LaunchEdit] = steamtools.apply_launch_options,
    extra_env: tuple[str, ...] = (),
) -> ActionResult:
    """Switch a game to native Wayland: its launch option, with Steam closed for the edit.

    ``extra_env`` are the ``NAME=value`` variables this game needs together with it.
    """
    return _change_runner(
        app_id,
        config_path=localconfig_path,
        run=run,
        steam_running=steam_running,
        wait_gone=wait_gone,
        binary=binary,
        game_running=game_running,
        change=lambda: apply(
            localconfig_path,
            app_id,
            lambda current: steamwayland.with_wayland(current, extra_env),
            steam_running_fn=steam_running,
        ),
        describe=lambda edit, note: (
            f"Native Wayland is on for app {app_id}.{note}"
            if edit.changed
            else f"Native Wayland was already on for app {app_id}.{note}"
        ),
        undo_of=lambda edit: steamwayland.LaunchUndo(edit.previous, edit.value),
    )


def restore_wayland(
    app_id: str | int,
    previous: steamwayland.LaunchUndo,
    *,
    localconfig_path: Path,
    run: Callable[[list[str]], int],
    steam_running: Callable[[], bool],
    wait_gone: Callable[[float], bool],
    binary: str | None = None,
    game_running: Callable[[], bool] | None = None,
) -> ActionResult:
    """Put a game's earlier launch options back exactly, if Steam still has what Nadare wrote.

    When the player changed them in Steam since, nothing is written: an undo must not delete
    their newer options. An undo saved before Nadare recorded what it wrote restores as before.
    """
    known = previous.written is not steamwayland.UNKNOWN

    def wanted(current: str | None) -> str | None:
        if known and current != previous.written:
            return current  # changed in Steam since: leave it
        return previous.previous

    def describe(edit: Any, note: str) -> str:
        if known and edit.previous != previous.written:
            return (
                f"The launch options of app {app_id} were changed in Steam after Nadare set "
                "them, so nothing was undone. Edit them in Steam: the game's Properties → "
                f"Launch options.{note}"
            )
        return f"The earlier launch options are back for app {app_id}.{note}"

    return _change_runner(
        app_id,
        config_path=localconfig_path,
        run=run,
        steam_running=steam_running,
        wait_gone=wait_gone,
        binary=binary,
        game_running=game_running,
        change=lambda: steamtools.apply_launch_options(
            localconfig_path, app_id, wanted, steam_running_fn=steam_running
        ),
        describe=describe,
        undo_of=lambda edit: None,
    )


# ---- finding the Proton a game uses ----------------------------------------

_EXPERIMENTAL = "proton_experimental"
#: Steam's older tool ids that are not simply ``<major>.0`` (their folder in ``common/``).
_OLD_PROTON = {"63": "6.3", "513": "5.13", "411": "4.11", "42": "4.2", "37": "3.7"}


def _display_name(tool: str) -> str | None:
    """Steam's ``common/`` folder name for a Proton tool id, or ``None``."""
    if tool == _EXPERIMENTAL:
        return "Proton - Experimental"
    if tool == "proton_hotfix":
        return "Proton Hotfix"
    if tool.startswith("proton_"):
        version = tool.removeprefix("proton_")
        if version in _OLD_PROTON:
            return f"Proton {_OLD_PROTON[version]}"
        if version.isdigit():
            return f"Proton {version}.0"
    return None


def _plain_name(name: str) -> bool:
    """A single folder name: no separators, no ``.``/``..``, no NUL."""
    return bool(name) and name not in (".", "..") and not any(c in name for c in "/\\\x00")


def _find_tool(
    name: str, *, steamapps_dirs: Iterable[Path], tool_dirs: Iterable[Path]
) -> Path | None:
    """The ``proton`` script of ``name`` in a tool folder or ``steamapps/common``."""
    if not _plain_name(name):
        return None
    for tool_dir in tool_dirs:
        candidate = Path(tool_dir) / name / "proton"
        if candidate.is_file():
            return candidate
    display = _display_name(name)
    if display is not None:
        for steamapps in steamapps_dirs:
            candidate = Path(steamapps) / "common" / display / "proton"
            if candidate.is_file():
                return candidate
    return None


def find_proton(
    app_id: str | int,
    compat_text: str,
    *,
    steamapps_dirs: Iterable[Path],
    tool_dirs: Iterable[Path],
) -> Path | None:
    """The ``proton`` script of the tool the game really runs with.

    The game's own CompatToolMapping entry decides; without one, Steam's default (the entry
    for app ``"0"``). A tool that is named but not installed gives ``None``: the installer must
    never run under a different Wine than the game. Only with no setting at all is the default
    Proton Experimental.
    """
    tool = ""
    if compat_text:
        entry = steamtools.read_compat_entry(compat_text, app_id)
        if entry is None:
            entry = steamtools.read_compat_entry(compat_text, "0")
        tool = entry.tool if entry is not None else ""
    if tool:
        return _find_tool(tool, steamapps_dirs=steamapps_dirs, tool_dirs=tool_dirs)
    return _find_tool(_EXPERIMENTAL, steamapps_dirs=steamapps_dirs, tool_dirs=tool_dirs)


# ---- running an installer in the game's prefix -----------------------------


def proton_run_command(
    *,
    proton: Path,
    compat_data: Path,
    client_path: Path,
    args: Iterable[str],
    base_env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """The Proton invocation and environment for a command inside one prefix."""
    argv = [str(proton), "run", *args]
    env = dict(base_env)
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(client_path)
    return argv, env


def install_prefix(
    app_id: str | int,
    key: str,
    *,
    prefix: Path,
    ensure_fn: Callable[[], object],
    running_blockers: Callable[[], object],
    backup_fn: Callable[[Path, Path], object],
    preflight: Callable[[], str | None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ActionResult:
    """Back up a game's prefix, then install one known prerequisite into it.

    Nothing runs while the game (or its wineserver) is using the prefix, and a
    backup is never removed, so a failed installer leaves a way back. ``preflight`` returns
    a plain reason the install cannot work at all (no display, ...); it runs before the
    backup so a doomed attempt does not leave a full copy of the prefix behind.
    """
    if key not in steamfix.PREREQUISITE_KEYS:
        raise BackendError(f"unknown prefix fix {key!r}")
    app_id = _numeric_id(app_id)

    blockers = running_blockers()
    if blockers:
        return ActionResult(
            False,
            "The game is still running, so its prefix cannot be changed. Close it and try again.",
        )

    if preflight is not None and (reason := preflight()):
        return ActionResult(False, reason)

    prefix = Path(prefix)
    dest = steamfix.plan_backup(prefix.parent, app_id, key)
    try:
        backup_fn(prefix, dest)
    except Exception as exc:  # noqa: BLE001 - reported as a plain line
        return ActionResult(False, f"Could not back up the prefix to {dest}: {exc}")
    if running_blockers():
        # The game was started while the copy ran: the backup may be torn and the prefix is live.
        return ActionResult(
            False,
            "The game started while the prefix was being backed up, so nothing was installed. "
            f"Close it and try again. The backup is at {dest}.",
            backup=dest,
        )
    if cancelled is not None and cancelled():
        return ActionResult(
            False,
            f"Cancelled before {key} was installed; nothing was changed. "
            f"The prefix backup is at {dest}.",
            backup=dest,
        )
    try:
        ensure_fn()
    except Cancelled:
        return ActionResult(
            False,
            f"Cancelled; {key} was not installed. The prefix backup is at {dest}.",
            backup=dest,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a plain line
        return ActionResult(
            False,
            f"Installing {key} failed ({exc}). The prefix backup is at {dest}.",
            backup=dest,
        )
    return ActionResult(
        True,
        f"Installed {key} into the game's prefix. The backup is at {dest}.",
        changed=True,
        backup=dest,
    )


__all__ = [
    "STEAM_CLOSE_TIMEOUT",
    "ActionResult",
    "find_proton",
    "install_prefix",
    "proton_run_command",
    "request_steam_install",
    "restore_runner",
    "set_runner",
    "unlock_h264",
]
