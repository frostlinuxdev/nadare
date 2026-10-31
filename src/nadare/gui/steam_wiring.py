# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Put the Steam Set up pieces together for a real window and a real machine.

The planner (``core.steamsetup``), the actions (``core.steamrun``) and the machine glue
(``core.steamglue``) stay free of Qt and of backend constants. This module is the one place that
may import backends: it supplies the WebView2 download that the direct and CZN backends already
use, and builds the dialog.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..core import (
    graphics,
    paths,
    process,
    steamfix,
    steamglue,
    steamrun,
    steamsetup,
    steamtools,
    steamundo,
    steamwayland,
    webview2,
)
from ..core.backends import BackendError
from ..core.events import Cancel
from .steam_setup import SteamSetupDialog


class SteamUnavailable(Exception):
    """Steam is not installed here; the message is meant for the user."""


def steam_app_id(profile: Mapping[str, Any]) -> str:
    """The numeric Steam app id of a ``steam`` profile, or ``""``."""
    if str(profile.get("backend") or "") != "steam":
        return ""
    extra = profile.get("extra")
    candidates = [profile.get("steam_app_id"), profile.get("app_id")]
    if isinstance(extra, Mapping):
        candidates += [extra.get("steam_app_id"), extra.get("app_id")]
    for value in candidates:
        text = str(value or "").strip()
        if text.isascii() and text.isdigit():
            return text
    return ""


def profile_value(profile: Mapping[str, Any], key: str) -> str:
    """A profile setting stored at the top level or under ``extra``, as text (``""`` if unset)."""
    extra = profile.get("extra")
    value = profile.get(key)
    if not value and isinstance(extra, Mapping):
        value = extra.get(key)
    return str(value or "").strip()


def install_webview2(
    app_id: str,
    *,
    root: Path,
    run_env: Mapping[str, str] | None = None,
    cancel: Cancel | None = None,
) -> steamrun.ActionResult:
    """Install WebView2 into one Steam game's prefix with the Proton that game uses.

    Reuses the verified download and the silent installer of the direct backend: the file is
    checked against the pinned hash or the Nadare index list before it runs.
    """
    dirs = steamglue.steamapps_dirs_for(root)
    try:
        location = steamfix.locate(app_id, steamapps_dirs=dirs)
    except BackendError as exc:
        return steamrun.ActionResult(False, str(exc))
    proton = steamrun.find_proton(
        app_id,
        steamglue.compat_text(root) or "",
        steamapps_dirs=dirs,
        tool_dirs=[steamglue.tool_dir(root)],
    )
    if proton is None:
        return steamrun.ActionResult(
            False,
            "The Proton this game uses was not found. Start the game once from Steam, then "
            "try again.",
        )
    prefix = location.prefix
    cancel = cancel if cancel is not None else Cancel()
    constant = webview2.PINNED_SHA256
    profile: dict[str, Any] = {}
    base_env = process.child_environ(run_env)

    def run_in_prefix(args: list[str]) -> int:
        argv, env = steamrun.proton_run_command(
            proton=proton,
            compat_data=prefix.parent,
            client_path=root,
            args=args,
            base_env={**base_env, "PROTON_ENABLE_WAYLAND": "0"},
        )
        return subprocess.run(argv, env=env, cwd=prefix.parent, check=False).returncode  # noqa: S603

    def verify(path: Path) -> None:
        webview2.verify(
            path,
            profile=profile,
            cancel=cancel,
            constant=constant,
            index_pins_fn=webview2.index_pins,
            overridden_fn=lambda: webview2.overridden(profile),
        )

    def download(url: str, tmp: Path, inst: Path) -> None:
        webview2.download(
            url, tmp, inst, verify_fn=verify, emit=lambda event: None, cancel=cancel
        )

    def ensure() -> None:
        webview2.ensure(
            prefix,
            profile=profile,
            cache_dir_fn=lambda: paths.cache_dir() / "czn",
            run_in_prefix=run_in_prefix,
            emit=lambda event: None,
            cancel=cancel,
            constant=constant,
            url=webview2.DEFAULT_URL,
            # a half install (files, no registration) is installed again, not skipped
            installed_fn=lambda: webview2.installed(prefix) and webview2.registered(prefix),
            pin_fn=lambda: webview2.resolve_pin(profile, constant),
            index_pins_fn=webview2.index_pins,
            index_source_fn=webview2.index_source,
            overridden_fn=lambda: webview2.overridden(profile),
            verify_fn=verify,
            download_fn=download,
            installer_environment=contextlib.nullcontext,
            activity_label=lambda label: contextlib.nullcontext(),
            explain=lambda text: text,
        )

    return steamrun.install_prefix(
        app_id,
        "webview2",
        prefix=prefix,
        ensure_fn=ensure,
        running_blockers=lambda: steamfix.running_blockers(app_id, prefix),
        backup_fn=steamfix.make_backup,
        preflight=_no_display_reason,
        cancelled=lambda: cancel.cancelled,
    )


def _no_display_reason() -> str | None:
    """The installer needs a display; say so before the prefix is copied, not after."""
    if graphics.has_display():
        return None
    return (
        "No display was found (DISPLAY and WAYLAND_DISPLAY are unset), and the installer "
        "needs one. Start Nadare from your desktop session and try again."
    )


def _vulkan_version() -> str | None:
    try:
        info = graphics.vulkan_info()
        # Without vulkaninfo only the loader's version is known, which says nothing about the GPU.
        return None if getattr(info, "loader_only", False) else info.api_version
    except Exception:  # noqa: BLE001 - an unknown version only means "do not call it old"
        return None


def build_dialog(
    parent,
    app_id: str,
    *,
    root: Path | None | str = "auto",
    vulkan_version_fn: Callable[[], str | None] = _vulkan_version,
    confirm_fn=None,
    run: Callable[[list[str]], int] = steamglue.run,
    restore_fn: Callable[..., steamrun.ActionResult] = steamrun.restore_runner,
    install_prefix_fn: Callable[[str, str], steamrun.ActionResult] | None = None,
    binary: str = "",
    async_plan: bool = True,
    steam_running: Callable[[], bool] = steamtools.steam_running,
    wait_gone: Callable[[float], bool] = steamglue.wait_until_gone,
) -> SteamSetupDialog:
    """The Set up dialog for one Steam game, wired to this machine's Steam.

    Raises :class:`SteamUnavailable` (with a plain message) when Steam is not installed.
    """
    steam_root = steamglue.steam_root() if root == "auto" else root
    if steam_root is None:
        raise SteamUnavailable(steamglue.NO_STEAM)
    steam_root = Path(steam_root)
    steam_command = steamglue.default_binary(steam_root, configured=binary)
    probed: list[str | None] = []

    def plan_fn() -> steamsetup.SetupPlan:
        # Runs on the thread pool (the Vulkan probe can take seconds); asked once per dialog.
        if not probed:
            probed.append(vulkan_version_fn())
        return steamglue.plan_for(app_id, root=steam_root, vulkan_version=probed[0])

    #: The token of the step that is running now (each step gets a fresh one).
    current_cancel: list[Cancel] = [Cancel()]

    def act_fn(item) -> steamrun.ActionResult:
        token = Cancel()
        current_cancel[0] = token

        def prefix_fn(game: str, key: str) -> steamrun.ActionResult:
            if install_prefix_fn is not None:
                return install_prefix_fn(game, key)
            if key == "webview2":
                return install_webview2(game, root=steam_root, cancel=token)
            return steamrun.ActionResult(False, "This fix is not available yet.")

        do_action = steamglue.make_actions(
            app_id,
            root=steam_root,
            run=run,
            steam_running=steam_running,
            wait_gone=wait_gone,
            install_prefix_fn=prefix_fn,
            binary=steam_command,
            cancel=token,
        )
        result = do_action(item)
        if item.action in steamundo.UNDOABLE and result.ok and result.changed:
            with contextlib.suppress(OSError):  # the change is done; a lost undo is not an error
                steamundo.save(app_id, item.action, result.previous)
        return result

    def cancel_fn() -> None:
        current_cancel[0].cancel()

    def undo_fn(previous) -> steamrun.ActionResult:
        if isinstance(previous, steamwayland.LaunchUndo):
            local = steamglue.active_localconfig_path(steam_root)
            if local is None:
                return steamrun.ActionResult(False, "Nadare could not find the Steam account file.")
            result = steamrun.restore_wayland(
                app_id,
                previous,
                localconfig_path=local,
                run=run,
                steam_running=steam_running,
                wait_gone=wait_gone,
                binary=steam_command,
            )
        else:
            result = restore_fn(
                app_id,
                previous,
                config_path=steamglue.config_vdf(steam_root),
                run=run,
                steam_running=steam_running,
                wait_gone=wait_gone,
                binary=steam_command,
            )
        if result.ok:
            with contextlib.suppress(OSError):
                steamundo.clear(app_id)
        return result

    return SteamSetupDialog(
        parent,
        plan_fn=plan_fn,
        act_fn=act_fn,
        confirm_fn=confirm_fn,
        undo_fn=undo_fn,
        async_plan=async_plan,
        app_id=str(app_id),
        initial_undo=steamundo.load(app_id),
        cancel_fn=cancel_fn,
    )
