# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Finding a plain system Wine, for the one-time generic-profile migration.

Generic "Wine / Proton game" profiles now run on Proton by default. A profile
that already had a Wine prefix keeps the Wine it was using: the migration in
:mod:`nadare.core.config` records it in ``wine_binary``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Wine builds probed, in order, when a legacy profile left ``wine_binary`` empty.
WINE_CANDIDATES = (
    "~/.local/share/frostlinuxlauncher/wine-tkg/bin/wine",
    "~/.config/heroic/tools/wine/wine-tkg-staging/bin/wine",
    "~/wine-tkg/bin/wine",
    "/usr/bin/wine",
)


def find_plain_wine() -> Path | None:
    """The first plain Wine found, or ``None``; never raises."""
    for candidate in WINE_CANDIDATES:
        try:
            path = Path(os.path.expanduser(candidate))
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    try:
        found = shutil.which("wine")
    except (OSError, ValueError):
        return None
    return Path(found) if found else None
