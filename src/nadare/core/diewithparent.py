# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""``python -m nadare.core.diewithparent PID COMMAND...``: become COMMAND, dying with PID (#1354).

Asks the kernel for SIGTERM when the starting thread's process dies, gives up if that process
is already gone (the race), then execs the real command. The signal survives ``exec``, so
``flatpak-spawn`` gets it and ``--watch-bus`` ends the game.

Started with ``-m``, never ``-c``: the frozen binary (AppImage, Flatpak) is not a Python
interpreter; its entry script runs ``-m`` modules (``packaging/nadare_entry.py``) after undoing
the bundle's ``LD_LIBRARY_PATH``, so the command does not load the bundle's private libraries.
Kept tiny: it runs once per game start.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass
    if os.getppid() != int(argv[0]):
        return 125
    # The environment is the one Nadare already built and filtered for the game (and the frozen
    # entry restored the host loader path): it passes on whole.
    os.execvp(argv[1], argv[1:])
    return 127  # not reached


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
