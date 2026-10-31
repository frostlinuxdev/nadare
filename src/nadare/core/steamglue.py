# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Steam Set up on a real machine: where Steam lives, running its commands, wiring the actions.

``steamsetup`` plans and ``steamrun`` acts through injected seams; this module supplies the real
ones that need only the standard library and Nadare's runner manager: finding Steam's folders,
starting commands without waiting on a client that stays up, waiting for Steam to close, and
making DW-Proton visible to Steam. The WebView2 download wiring needs backend constants and
lives in ``gui/steam_wiring.py`` (core must not import backends).
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from . import process, runners, steamfix, steamrun, steamsetup, steamtools, steamwayland
from .backends import BackendError
from .events import Cancel, Cancelled

_ROOTS = (
    Path(".local") / "share" / "Steam",
    Path(".steam") / "steam",
    Path(".steam") / "root",
    Path(".var") / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
)

NO_STEAM = (
    "Steam was not found. Nadare does not install Steam: get it from your distribution or "
    "from store.steampowered.com, sign in once, then try again."
)


# ---- finding Steam ------------------------------------------------------------------------
def steam_root(*, home: Path | None = None) -> Path | None:
    """The first Steam folder (native, then Flatpak) that holds a ``steamapps`` folder."""
    base = Path(home) if home is not None else Path.home()
    for relative in _ROOTS:
        candidate = base / relative
        if (candidate / "steamapps").is_dir():
            return candidate
    return None


FLATPAK_STEAM = "flatpak run com.valvesoftware.Steam"


def default_binary(
    root: Path | None, *, home: Path | None = None, configured: str = ""
) -> str | None:
    """The command that starts this Steam: the profile's own setting, else Flatpak's when the
    Steam found is the Flatpak one, else ``None`` (plain ``steam`` on the path)."""
    if configured.strip():
        return configured.strip()
    if root is None:
        return None
    base = Path(home) if home is not None else Path.home()
    flatpak = base / ".var" / "app" / "com.valvesoftware.Steam"
    root = Path(root)
    return FLATPAK_STEAM if root == flatpak or flatpak in root.parents else None


def config_vdf(root: Path) -> Path:
    return Path(root) / "config" / "config.vdf"


def tool_dir(root: Path) -> Path:
    return Path(root) / "compatibilitytools.d"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def compat_text(root: Path) -> str | None:
    """The text of ``config.vdf`` (the compatibility-tool mapping), or ``None``."""
    return _read(config_vdf(root))


def localconfig_texts(root: Path) -> list[str]:
    """The ``localconfig.vdf`` text of every account that has signed in on this machine."""
    texts: list[str] = []
    for path in sorted((Path(root) / "userdata").glob("*/config/localconfig.vdf")):
        text = _read(path)
        if text is not None:
            texts.append(text)
    return texts


def active_localconfig_path(root: Path) -> Path | None:
    """The ``localconfig.vdf`` of the account that is signed in, or ``None`` when unknown.

    ``loginusers.vdf`` says which account signed in last. Without it, a machine with a single
    account is that account; with several and no way to tell, nothing is assumed, because
    another account's setting says nothing about this one.
    """
    root = Path(root)
    users = _read(root / "config" / "loginusers.vdf") or ""
    steam_id = steamtools.most_recent_user(users)
    number = steamtools.account_id(steam_id) if steam_id else None
    if number is not None:
        path = root / "userdata" / str(number) / "config" / "localconfig.vdf"
        return path if path.is_file() else None
    accounts = sorted((root / "userdata").glob("*/config/localconfig.vdf"))
    return accounts[0] if len(accounts) == 1 else None


def active_localconfig_texts(root: Path) -> list[str]:
    """The ``localconfig.vdf`` text of the account that is signed in (a list of one, or none).

    Without a way to tell the account, nothing is assumed (the H.264 step then asks), because
    another account's setting says nothing about this one.
    """
    path = active_localconfig_path(root)
    text = _read(path) if path is not None else None
    return [text] if text is not None else []


def steamapps_dirs_for(root: Path) -> list[Path]:
    """``steamapps`` of this Steam root plus every extra library it lists."""
    main = Path(root) / "steamapps"
    dirs = [main]
    text = _read(main / "libraryfolders.vdf") or ""
    for match in steamfix._LIBRARY_PATH_RE.finditer(text):
        library = Path(match.group(1).replace("\\\\", "\\")).expanduser()
        if library.is_absolute() and library / "steamapps" not in dirs:
            dirs.append(library / "steamapps")
    return dirs


# ---- making a runner visible to Steam -----------------------------------------------------
def ensure_steam_tool(root: Path, runner: Path) -> str:
    """Make ``runner`` selectable in Steam; returns the name to write into ``config.vdf``.

    The name is the folder name (it equals the tool's internal name). A runner outside Steam's
    ``compatibilitytools.d`` is linked in under its own name. An existing different entry of
    that name is never replaced.
    """
    runner = Path(runner)
    tools = tool_dir(root)
    if runner.parent.resolve() == tools.resolve():
        return runner.name
    tools.mkdir(parents=True, exist_ok=True)
    link = tools / runner.name
    if link.is_symlink() and link.resolve() == runner.resolve():
        return runner.name
    if link.exists() or link.is_symlink():
        raise BackendError(
            f"{link} already exists and is a different tool; Nadare will not replace it. "
            "Remove or rename it, then try again."
        )
    try:
        link.symlink_to(runner)
    except OSError as exc:
        raise BackendError(f"could not make {runner.name} visible to Steam: {exc}") from exc
    return runner.name


# ---- running things -----------------------------------------------------------------------
def run(argv: list[str], *, settle: float = 5.0) -> int:
    """Start ``argv`` and report how it began.

    A command that ends within ``settle`` seconds returns its exit code. One that is still up
    (Steam itself stays running when this command is what starts it) counts as started: it is
    left alone and reaped in the background. A program that cannot be started returns 127.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is a list built by Nadare, no shell
            argv,
            env=process.child_environ(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return 127
    try:
        return proc.wait(timeout=settle)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, name="nadare-steam-reaper", daemon=True).start()
        return 0


def wait_until_gone(
    timeout: float,
    *,
    running: Callable[[], bool] = steamtools.steam_running,
    poll: float = 0.5,
) -> bool:
    """Wait until Steam is not running; ``False`` when it still is after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while running():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


# ---- the planner and the dispatcher -------------------------------------------------------------
def wait_until(
    predicate: Callable[[], bool],
    timeout: float,
    *,
    poll: float = 0.5,
) -> bool:
    """``True`` as soon as ``predicate`` holds; ``False`` if it still does not after ``timeout``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def plan_for(
    app_id: str | int,
    *,
    root: Path | None,
    vulkan_version: str | None,
    wayland_session: bool | None = None,
) -> steamsetup.SetupPlan:
    """The Set up checklist read from the real Steam files under ``root``."""
    if root is None:
        raise BackendError(NO_STEAM)
    dirs = steamapps_dirs_for(root)
    local = active_localconfig_texts(root)
    compat = compat_text(root)
    session = steamwayland.wayland_session() if wayland_session is None else wayland_session
    proton = None
    if session:  # only a Wayland session is offered the step, so only then look at the runner
        proton = steamrun.find_proton(
            app_id, compat or "", steamapps_dirs=dirs, tool_dirs=[tool_dir(root)]
        )
    return steamsetup.plan_setup(
        app_id,
        steamapps_dirs=dirs,
        localconfig_texts=local,
        compat_text=compat,
        vulkan_version=vulkan_version,
        wayland_session=session,
        runner_has_wayland=steamwayland.runner_has_wayland(proton),
        launch_options=steamtools.read_launch_options(local[0], app_id) if local else None,
    )


_CANCELLED = "Cancelled. Nothing was changed."


def make_actions(
    app_id: str | int,
    *,
    root: Path | None,
    run: Callable[[list[str]], int] = run,
    wait_gone: Callable[[float], bool] = wait_until_gone,
    steam_running: Callable[[], bool] = steamtools.steam_running,
    binary: str | None = None,
    ensure_runner_fn: Callable[[], Path] | None = None,
    install_prefix_fn: Callable[[str, str], steamrun.ActionResult] | None = None,
    confirm_timeout: float = 20.0,
    poll: float = 0.5,
    cancel: Cancel | None = None,
) -> Callable[[steamsetup.SetupItem], steamrun.ActionResult]:
    """One function that performs a checklist item's named action. Unknown actions are refused."""
    key = str(app_id)

    def act(item: steamsetup.SetupItem) -> steamrun.ActionResult:
        action = item.action
        try:
            if action == "steam_install":
                return steamrun.request_steam_install(key, run=run, binary=binary)
            if action == "unlock_h264":
                result = steamrun.unlock_h264(run=run, binary=binary)
                if not result.ok or root is None:
                    return result
                # Steam answers at once but writes its settings a moment later: report the
                # step as done only when it has really been recorded.
                if wait_until(
                    lambda: steamsetup.h264_unlocked(active_localconfig_texts(root)),
                    confirm_timeout,
                    poll=poll,
                ):
                    return result
                return steamrun.ActionResult(
                    False,
                    "Steam was asked to unlock H.264 but has not confirmed it yet. Wait a "
                    "moment and open Set up again.",
                )
            if action == "set_runner":
                if root is None:
                    return steamrun.ActionResult(False, NO_STEAM)
                game = steamsetup.game_for(key)
                family = game.runner if game is not None else "dwproton"
                fetch = ensure_runner_fn or (
                    lambda: runners.ensure_pinned(family, cancel=cancel)
                )
                try:
                    runner = fetch()
                except Cancelled:
                    return steamrun.ActionResult(False, _CANCELLED)
                if cancel is not None and cancel.cancelled:
                    return steamrun.ActionResult(False, _CANCELLED)  # before Steam is touched
                tool = ensure_steam_tool(root, runner)
                return steamrun.set_runner(
                    key,
                    tool,
                    config_path=config_vdf(root),
                    run=run,
                    steam_running=steam_running,
                    wait_gone=wait_gone,
                    binary=binary,
                )
            if action == "set_wayland":
                path = active_localconfig_path(root) if root is not None else None
                if path is None:
                    return steamrun.ActionResult(
                        False,
                        "Nadare could not tell which Steam account is signed in, so it cannot "
                        "change that account's launch options.",
                    )
                game = steamsetup.game_for(key)
                return steamrun.set_wayland(
                    key,
                    extra_env=game.launch_env if game is not None else (),
                    localconfig_path=path,
                    run=run,
                    steam_running=steam_running,
                    wait_gone=wait_gone,
                    binary=binary,
                )
            if action.startswith("install_prefix:"):
                prefix_key = action.split(":", 1)[1]
                if install_prefix_fn is None:
                    return steamrun.ActionResult(False, "This fix is not available yet.")
                return install_prefix_fn(key, prefix_key)
        except Exception as exc:  # noqa: BLE001 - shown to the user as a plain line
            return steamrun.ActionResult(False, str(exc) or exc.__class__.__name__)
        return steamrun.ActionResult(False, f"Unknown step {action!r}; nothing was done.")

    return act


def all_steamapps(root: Path | None) -> Iterable[Path]:
    return steamapps_dirs_for(root) if root is not None else []
