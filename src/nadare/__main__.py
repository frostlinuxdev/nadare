# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""``python -m nadare`` entry point.

The argument parser lives in :func:`nadare.app.build_parser`; this module is the
thin, importable entry that package metadata and ``python -m`` call. Run
``nadare --help`` for the subcommands (list, status, install, update, login,
launch, stop, verify).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from .app import build_parser, run_cli


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    return run_cli(argv)


__all__ = ["main", "build_parser"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
