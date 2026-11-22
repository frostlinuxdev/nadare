# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep DW-Proton's FSR4 upgrade only where it can do something.

DW-Proton turns its FSR4 upgrade on for every game (``compat_config.add("fsr4")``). Its setup
downloads ``proton-upscalers/manifest.json`` from GitHub on **every** launch (10 s timeout:
~0.25 s on a good line, up to 10 s on a bad one) and copies ``amdxcffx64.dll`` into the prefix.
The upgrade itself is only active by default on GPUs with FP8 (AMD RDNA4, RX 9000); RDNA3 and
older, NVIDIA and Intel get nothing from it unless the user forces it.

So Nadare sets ``PROTON_FSR4_UPGRADE=0`` when every GPU it sees is an NVIDIA, Intel or pre-RDNA4
AMD one. An RDNA4 card, a GPU it does not know, or no answer leaves Proton's default. A
``PROTON_FSR4_UPGRADE`` the user set (exported or in the profile's Environment) always wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

DRM_ROOT = Path("/sys/class/drm")
ENV = "PROTON_FSR4_UPGRADE"

_VENDORS = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}
#: amdgpu's graphics IP (GC) major version: 12 = RDNA4, the first with FP8.
_RDNA4_GC = 12
_USELESS = frozenset({"amd", "nvidia", "intel"})


def _amd_kind(device: Path) -> str:
    gc = device / "ip_discovery" / "die" / "0" / "GC" / "0" / "major"
    try:
        major = int(gc.read_text().strip())
    except (OSError, ValueError):
        return "amd"  # RDNA4 needs a kernel that has IP discovery in sysfs
    return "amd-rdna4" if major >= _RDNA4_GC else "amd"


def gpu_kinds(root: Path | None = None) -> set[str] | None:
    """The kinds of GPU in ``/sys/class/drm``: ``amd``, ``amd-rdna4``, ``nvidia``, ``intel`` or
    ``other``. None when no card can be read."""
    root = DRM_ROOT if root is None else root
    kinds: set[str] = set()
    try:
        cards = [p for p in root.iterdir() if p.name.startswith("card") and p.name[4:].isdigit()]
    except OSError:
        return None
    for card in cards:
        device = card / "device"
        try:
            vendor = (device / "vendor").read_text().strip().lower()
        except OSError:
            continue
        kind = _VENDORS.get(vendor, "other")
        kinds.add(_amd_kind(device) if kind == "amd" else kind)
    return kinds or None


def upgrade_env(env: Mapping[str, str]) -> dict[str, str]:
    """``{"PROTON_FSR4_UPGRADE": "0"}`` when the upgrade cannot help here, else ``{}``."""
    if ENV in env:
        return {}
    kinds = gpu_kinds()
    if not kinds or not kinds <= _USELESS:
        return {}
    return {ENV: "0"}
