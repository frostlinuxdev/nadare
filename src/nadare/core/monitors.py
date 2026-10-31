# SPDX-License-Identifier: GPL-3.0-or-later
"""Connected monitors and the per-game "open on this monitor" launch variable.

Wine's Wayland driver reads ``WAYLANDDRV_PRIMARY_MONITOR=<connector>`` (``DP-1``,
``HDMI-A-1``, ``eDP-1``) to decide which output is primary. It does nothing under
XWayland. An *unknown* name does not fall back to the compositor's primary: the
driver then takes the left-most monitor, so :func:`launch_env` drops a name that is
not connected instead of passing it on.

The list comes from ``/sys/class/drm``, which is world-readable and the same on every
desktop and in a Flatpak, so no D-Bus or desktop tool is needed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import Option

log = logging.getLogger(__name__)

DRM_ROOT = Path("/sys/class/drm")
ENV_VAR = "WAYLANDDRV_PRIMARY_MONITOR"

_ENTRY = re.compile(r"^card\d+-(.+)$")
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

#: The per-game setting, shown on every backend that runs Wine.
OPTION = Option(
    "monitor",
    "Monitor",
    "monitor",
    "Which monitor the game opens on. Works only with native Wayland (Display backend "
    "Wayland); on X11 use your desktop's window rules. Auto leaves it to the desktop.",
)


@dataclass(frozen=True)
class Monitor:
    name: str
    size: str = ""
    enabled: bool = True

    @property
    def label(self) -> str:
        detail = ", ".join(part for part in (self.size, "" if self.enabled else "off") if part)
        return f"{self.name} ({detail})" if detail else self.name


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def connected(root: Path | None = None) -> list[Monitor]:
    """Connected monitors, one per connector name, sorted by name."""
    root = DRM_ROOT if root is None else root
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    found: dict[str, Monitor] = {}
    for entry in entries:
        match = _ENTRY.match(entry.name)
        if not match:
            continue
        name = match.group(1)
        if name.startswith("Writeback-") or "Virtual-" in name or name in found:
            continue
        if _read(entry / "status") != "connected":
            continue
        modes = _read(entry / "modes").splitlines()
        found[name] = Monitor(
            name=name,
            size=modes[0].strip() if modes else "",
            enabled=_read(entry / "enabled") != "disabled",
        )
    return [found[name] for name in sorted(found)]


def _valid(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _NAME.match(text) and text.lower() != "auto" else ""


def chosen(profile: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    """The monitor this game should use: its own choice, else the global default."""
    return _valid(profile.get("monitor")) or _valid(settings.get("default_monitor"))


def apply_env(env: dict[str, str], ctx: Any, root: Path | None = None) -> None:
    """Add the chosen monitor to a game's ``env`` (a profile Environment line still wins)."""
    env.update(launch_env(ctx.profile, ctx.config.data, root))


def launch_env(
    profile: Mapping[str, Any],
    settings: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, str]:
    """``{WAYLANDDRV_PRIMARY_MONITOR: name}`` for the chosen monitor, else ``{}``."""
    name = chosen(profile, settings)
    if not name:
        return {}
    root = DRM_ROOT if root is None else root
    if root.is_dir() and name not in {m.name for m in connected(root)}:
        log.warning("monitor %s is not connected; leaving the choice to the desktop", name)
        return {}
    return {ENV_VAR: name}
