#!/usr/bin/env python3
"""Render Add Game install-location states for review (Qt offscreen)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtWidgets import QApplication

from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} OUTDIR", file=sys.stderr)
        return 2
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])

    def render(name: str, *, kind: str = "wine", mode: str = "collapsed") -> None:
        dialog = AddGameDialog(
            Config.load(output / "config.json"), data_root=output / "data", kind=kind
        )
        dialog.show()
        app.processEvents()
        if mode != "collapsed":
            dialog.location_toggle.click()
        if mode == "advanced":
            dialog.prefix_toggle.click()
        elif mode == "import":
            dialog.set_mode(True)
        dialog.adjustSize()
        app.processEvents()
        dialog.grab().save(str(output / name))
        dialog.close()
        dialog.deleteLater()
        app.processEvents()

    render("collapsed.png")
    render("expanded.png", mode="expanded")
    render("advanced-prefix.png", mode="advanced")
    render("import.png", mode="import")
    render("czn-expanded.png", kind="stove-czn", mode="expanded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
