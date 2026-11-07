# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Set up checklist for a Steam game (``docs/STEAM-SETUP.md``): what is done, what is next.

A pure planner. It reads Steam's own files and says, per game, which of these steps are
finished, still to do, blocked or only offered, least invasive first:

1. the game is installed in Steam (Steam does the download),
2. Steam's H.264 unlock, for games whose only problem is their opening video,
3. what the game's Proton prefix is missing (WebView2, Visual C++),
4. a better runner (DW-Proton), recommended on a Vulkan older than 1.3 and otherwise optional.

Nothing here changes anything; the caller runs the action it names, after asking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import steamfix, steamtools, steamwayland
from .backends import BackendError

#: The runner Set up offers (the tested pin, see ``core/runners.RUNNERS``).
RUNNER_PREFIX = "dwproton"
#: family key -> (name shown to the user, prefix of the Steam tool folder it installs as)
RUNNER_FAMILIES = {
    "dwproton": ("DW-Proton", RUNNER_PREFIX),
    "ge": ("GE-Proton", "GE-Proton"),
}

STATE_DONE = "done"
STATE_TODO = "todo"
STATE_BLOCKED = "blocked"
STATE_OPTIONAL = "optional"
STATE_RECOMMENDED = "recommended"


@dataclass(frozen=True)
class SteamGame:
    """One game's evidence-backed entry. Adding a game is one row in :data:`GAMES`."""

    app_id: str
    game: str
    #: Prefix prerequisites it fails without (keys of ``steamfix.PREREQUISITE_KEYS``).
    needs: tuple[str, ...] = ()
    #: The only trouble is video: Steam's H.264 unlock fixes it.
    video: bool = False
    #: ``False`` until the fix has been seen working on a real Steam.
    verified: bool = False
    note: str = ""
    #: The runner family this game's runner step offers (a key of ``runners``): DW-Proton unless the
    #: game is known to need another build.
    runner: str = "dwproton"
    #: ``NAME=value`` launch variables the native Wayland step adds for this game (``UMU_ID=0``).
    launch_env: tuple[str, ...] = ()


#: Data, not code. Each ``verified`` row was reproduced on a real Steam on 2026-10-31
#: (evidence in ``docs/STEAM-SETUP.md``).
GAMES: tuple[SteamGame, ...] = (
    SteamGame("5019180", "Epic Seven", needs=(steamfix.KEY_WEBVIEW2,), verified=True),
    SteamGame("3224770", "Umamusume: Pretty Derby", video=True, verified=True),
    SteamGame(
        "3557620",
        "Blue Archive",
        verified=True,
        note="starts on Proton Experimental (tested with the H.264 unlock already on); "
        "a runner is only an option",
    ),
    SteamGame(
        "2668080",
        "Snowbreak: Containment Zone",
        video=True,
        note="reported white screen or video trouble; not tested here yet",
    ),
    SteamGame(
        "3308670",
        "Girls' Frontline 2: Exilium (Haoplay)",
        needs=(steamfix.KEY_WEBVIEW2,),
        note="the Haoplay build; its login SDK (haoplay-pcsdk, PnSDKCPP.dll) embeds WebView2. "
        "Its sign-in window rendered and took clicks on GE-Proton11-7 with native Wayland; "
        "DW-Proton 11.0-15 froze there. Nothing past the sign-in is tested",
        runner="ge",
        launch_env=("UMU_ID=0",),
    ),
    SteamGame(
        "3347400",
        "Girls' Frontline 2: Exilium (Darkwinter)",
        note="the Darkwinter build is region-locked on the test account, so it is untested; "
        "its sign-in does not use WebView2 (reported by a player)",
    ),
)


def game_for(app_id: str | int) -> SteamGame | None:
    key = str(app_id).strip()
    return next((game for game in GAMES if game.app_id == key), None)


@dataclass(frozen=True)
class SetupItem:
    key: str
    label: str
    state: str
    detail: str = ""
    #: What the caller runs: ``steam_install``, ``unlock_h264``, ``install_prefix:<key>`` or
    #: ``set_runner``.
    action: str = ""
    needs_steam_closed: bool = False
    needs_steam_running: bool = False
    #: Plain text for the confirm dialog (empty when no confirmation is needed).
    warning: str = ""
    #: What pressing the button for this step does, in the imperative ("" when it has none).
    action_label: str = ""


@dataclass(frozen=True)
class SetupPlan:
    app_id: str
    game: str
    verified: bool
    note: str
    items: tuple[SetupItem, ...]

    @property
    def next_item(self) -> SetupItem | None:
        """The first step still to do, else the recommended one (``None`` when nothing is left).

        An optional runner is never pushed.
        """
        for wanted in (STATE_TODO, STATE_RECOMMENDED):
            found = next((item for item in self.items if item.state == wanted), None)
            if found is not None:
                return found
        return None

    @property
    def ready(self) -> bool:
        """Every needed step is done. A merely optional runner never blocks this."""
        return all(item.state in (STATE_DONE, STATE_OPTIONAL) for item in self.items)


# ---- reading Steam's settings --------------------------------------------------------------
def h264_unlocked(localconfig_texts: Iterable[str]) -> bool:
    """Whether any account's ``localconfig.vdf`` has the H.264 unlock switched on."""
    return any(
        steamtools.read_flag(text, "UnlockedH264Feature") == "1" for text in localconfig_texts
    )


_VERSION = re.compile(r"^\s*(\d+)\.(\d+)")


def vulkan_at_least_1_3(version: str | None) -> bool | None:
    """Whether the Vulkan API version is 1.3 or newer; ``None`` when it is not known."""
    match = _VERSION.match(version or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2))) >= (1, 3)


_H264_WARNING = (
    "Nadare will ask Steam to unlock H.264 video. This is a setting of your Steam account, "
    "not only of this game: Steam keeps it, and it lets Steam play videos such as the game's "
    "opening movie. Steam starts if it is not running. It changes nothing in the game's files."
)


def _prefix_warning(label: str) -> str:
    return (
        f"Nadare will back up this game's Proton prefix and then install {label} into it. "
        "Close the game first; Nadare refuses to continue while it runs. The backup is a copy "
        "next to the prefix and is never deleted automatically, so you can go back to it."
    )


_UNDO = (
    "The previous setting is saved: the Undo button in this window (also when you open it again "
    "later) puts it back, and you can change it yourself in Steam (the game's Properties, "
    "Compatibility)."
)
_RUNNER_WARNING = (
    "Nadare will change this game's Steam compatibility setting to DW-Proton. Reason: this "
    "machine's Vulkan is older than 1.3, which Steam's default Proton needs, and DW-Proton "
    "switches by itself to a graphics layer built for older Vulkan (DXVK-Sarek). Steam has to "
    "be closed for the change: if it is running it is closed first (a running download stops) "
    "and started again afterwards. " + _UNDO
)
_GE_WARNING = (
    "Nadare will change this game's Steam compatibility setting to GE-Proton, a community build. "
    "Reason: this game's sign-in window is a web page drawn inside the game, and only GE-Proton's "
    "Wine-Wayland child-window rendering showed it and took clicks in testing (DW-Proton 11.0-15 "
    "froze on it). Use it together with the native Wayland step. Steam has to be closed for the "
    "change: if it is running it is closed first (a running download stops) and started again "
    "afterwards. " + _UNDO
)
_RUNNER_OPTIONAL_WARNING = (
    "Nadare will change this game's Steam compatibility setting to DW-Proton, a community "
    "build with extra fixes. You only need this if the game does not start on Steam's default "
    "Proton. Steam has to be closed for the change: if it is running it is closed first (a "
    "running download stops) and started again afterwards. " + _UNDO
)


def _wayland_warning(extra: tuple[str, ...]) -> str:
    also = (
        f" and {', '.join(extra)} (the id Nadare gives this game: no game-specific fixes)"
        if extra
        else ""
    )
    return _WAYLAND_WARNING.format(also=also)


_WAYLAND_WARNING = (
    "Nadare will switch this game to native Wayland: it adds PROTON_ENABLE_WAYLAND=1{also} to the "
    "game's launch options in Steam (anything you already wrote there stays). This gets rid of "
    "the flicker some games show under X11. Side effects to know about: the Steam overlay may "
    "not appear and Steam Input (controller support) can misbehave under native Wayland; if "
    "that happens, press Undo. It is a setting of your Steam account for this game "
    "only. Steam has to be closed for the change: if it is running it is closed first (a "
    "running download stops) and started again afterwards. The earlier launch options are saved: "
    "the Undo button in this window (also when you open it again later) puts them back, and "
    "you can edit them in Steam (the game's Properties, General, Launch options)."
)


def plan_setup(
    app_id: str | int,
    *,
    steamapps_dirs: Iterable[Path] | None = None,
    localconfig_texts: Iterable[str] = (),
    compat_text: str | None = None,
    vulkan_version: str | None = None,
    wayland_session: bool = False,
    runner_has_wayland: bool = False,
    launch_options: str | None = None,
) -> SetupPlan:
    """The checklist for ``app_id``. Raises :class:`BackendError` for an unknown game."""
    game = game_for(app_id)
    if game is None:
        raise BackendError(f"no known Steam setup for app {app_id}")
    items: list[SetupItem] = []

    try:
        library = steamfix.find_library(game.app_id, steamapps_dirs=steamapps_dirs)
        installed = True
    except BackendError as exc:
        library = None
        installed = False
        install_detail = str(exc)
    items.append(
        SetupItem(
            "installed",
            "Installed in Steam",
            STATE_DONE if installed else STATE_TODO,
            "" if installed else install_detail,
            "" if installed else "steam_install",
            action_label="" if installed else "Install in Steam",
        )
    )

    def blocked(key: str, label: str) -> SetupItem:
        return SetupItem(key, label, STATE_BLOCKED, "Install the game in Steam first.")

    if game.video:
        if not installed:
            items.append(blocked("h264", "H.264 video unlocked"))
        else:
            on = h264_unlocked(localconfig_texts)
            items.append(
                SetupItem(
                    "h264",
                    "H.264 video unlocked",
                    STATE_DONE if on else STATE_TODO,
                    "" if on else "Lets Steam play the game's opening video.",
                    "" if on else "unlock_h264",
                    needs_steam_running=not on,
                    warning="" if on else _H264_WARNING,
                    action_label="" if on else "Unlock H.264 video",
                )
            )

    for key in game.needs:
        label = steamfix._LABELS.get(key, key)
        item_key = f"prefix:{key}"
        if not installed:
            items.append(blocked(item_key, label))
            continue
        prefix = Path(library) / "compatdata" / game.app_id / "pfx"
        if not prefix.is_dir():
            items.append(
                SetupItem(
                    item_key,
                    label,
                    STATE_BLOCKED,
                    "Start the game once from Steam and close it (an error is expected), "
                    "then try again.",
                )
            )
            continue
        present = any(p.present for p in steamfix.check_prerequisites(prefix) if p.key == key)
        items.append(
            SetupItem(
                item_key,
                label,
                STATE_DONE if present else STATE_TODO,
                "" if present else f"{label} is missing from the game's prefix.",
                "" if present else f"install_prefix:{key}",
                warning="" if present else _prefix_warning(label),
                action_label="" if present else f"Install {label}",
            )
        )

    old_vulkan = vulkan_at_least_1_3(vulkan_version) is False
    using = ""
    if compat_text:
        entry = steamtools.read_compat_entry(compat_text, game.app_id)
        using = entry.tool if entry is not None else ""
    name, tool_prefix = RUNNER_FAMILIES.get(game.runner, RUNNER_FAMILIES["dwproton"])
    label = f"{name} runner"
    if not installed:
        items.append(blocked("runner", label))
    elif using.startswith(tool_prefix):
        items.append(SetupItem("runner", label, STATE_DONE, f"Using {using}."))
    elif game.runner == "ge":
        items.append(
            SetupItem(
                "runner",
                label,
                STATE_RECOMMENDED,
                "Recommended: this game's sign-in window only works with GE-Proton's Wayland "
                "rendering (DW-Proton froze on it).",
                "set_runner",
                needs_steam_closed=True,
                warning=_GE_WARNING,
                action_label=f"Use {name}",
            )
        )
    else:
        items.append(
            SetupItem(
                "runner",
                label,
                STATE_RECOMMENDED if old_vulkan else STATE_OPTIONAL,
                "Recommended: this machine's Vulkan is older than 1.3."
                if old_vulkan
                else "Optional: only if the game does not start on Steam's default Proton.",
                "set_runner",
                needs_steam_closed=True,
                warning=_RUNNER_WARNING if old_vulkan else _RUNNER_OPTIONAL_WARNING,
                action_label=f"Use {name}",
            )
        )

    if wayland_session:
        wayland_label = "Native Wayland"
        if not installed:
            items.append(blocked("wayland", wayland_label))
        elif steamwayland.has_wayland(launch_options):
            if steamwayland.has_variables(launch_options, game.launch_env):
                items.append(
                    SetupItem(
                        "wayland", wayland_label, STATE_DONE, "On in this game's launch options."
                    )
                )
            else:
                # On, but the game's own variable is missing: not done (#970).
                items.append(
                    SetupItem(
                        "wayland",
                        wayland_label,
                        STATE_OPTIONAL,
                        "On, but this game also needs "
                        + ", ".join(game.launch_env)
                        + " in its launch options.",
                        "set_wayland",
                        needs_steam_closed=True,
                        warning=_wayland_warning(game.launch_env),
                        action_label="Add the missing variable",
                    )
                )
        elif not runner_has_wayland:
            items.append(
                SetupItem(
                    "wayland",
                    wayland_label,
                    STATE_OPTIONAL,
                    "This game's runner has no Wayland driver (Steam's default Proton has "
                    "none). Use the DW-Proton step first to get rid of X11 flicker.",
                )
            )
        else:
            items.append(
                SetupItem(
                    "wayland",
                    wayland_label,
                    STATE_OPTIONAL,
                    "Optional: gets rid of flicker some games show under X11.",
                    "set_wayland",
                    needs_steam_closed=True,
                    warning=_wayland_warning(game.launch_env),
                    action_label="Use native Wayland",
                )
            )

    return SetupPlan(game.app_id, game.game, game.verified, game.note, tuple(items))
