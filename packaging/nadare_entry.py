# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Frozen-app entry point used by the PyInstaller spec.

The bundled ``nadare`` binary is primarily the desktop launcher, so with no
arguments it starts the GUI. Any argument is passed through to the CLI
(``nadare list``, ``nadare status -p czn``, ...), which keeps one artifact for
both front ends. A source checkout is unaffected: there the ``nadare`` console
script and ``python -m nadare`` remain CLI-first.
"""

from __future__ import annotations

import os
import sys


def _smoke_exit_ms() -> int | None:
    """Return the auto-quit delay in ms when ``NADARE_SMOKE_EXIT_MS`` is set.

    This is a packaging/test hook: it lets CI start the real GUI under an
    offscreen Qt platform and have it exit on its own, without touching the
    GUI code or requiring a display.
    """
    raw = os.environ.get("NADARE_SMOKE_EXIT_MS")
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _run_gui() -> int:
    from nadare.gui import run

    ms = _smoke_exit_ms()
    if ms is not None:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        QTimer.singleShot(ms, app.quit)
    return run()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if getattr(sys, "frozen", False):
        from nadare.core.process import restore_host_env

        restore_host_env()

    # PyInstaller-frozen interpreter shim. The CZN backend spawns its helper
    # modules (auth/update/runners) with ``sys.executable -m <module>``; see
    # nadare/backends/czn/backend.py. A frozen bootloader has no ``-m``
    # support, so honour it here exactly like CPython would, before the CLI
    # parser ever sees the arguments. In a source checkout this branch is
    # never reached (``sys.executable`` is the real Python interpreter).
    if args[:1] == ["-m"] and len(args) >= 2:
        import runpy

        module, *rest = args[1:]
        sys.argv = [module, *rest]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0

    # Explicit escape hatches for packagers and testing.
    if args[:1] == ["--gui"]:
        args = args[1:]
        return _run_gui()
    force_cli = args[:1] == ["--cli"]
    if force_cli:
        args = args[1:]

    if not args and not force_cli:
        try:
            return _run_gui()
        except Exception as exc:  # noqa: BLE001 - fall back to the CLI
            print(
                f"nadare: GUI unavailable ({exc}); run `nadare --help` for the CLI", file=sys.stderr
            )
            return 1

    from nadare.__main__ import main as cli

    return cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
