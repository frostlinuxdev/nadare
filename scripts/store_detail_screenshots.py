#!/usr/bin/env python3
"""Render representative store detail pages offscreen for review."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.store_detail import StoreDetail
from nadare.gui.theme import apply_theme


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} OUTDIR", file=sys.stderr)
        return 2
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    apply_theme(app)

    wanted = ("direct-pgr", "launcher-nikke", "stove-czn", "fgo-lepton")
    available = {entry.kind: entry for entry in entries()}
    for kind in wanted:
        source = available[kind]
        for width in (520, 1000):
            entry = StoreEntry(**{
                **{name: getattr(source, name) for name in StoreEntry.__dataclass_fields__},
                "installed": kind == "stove-czn",
            })
            widget = StoreDetail(entry)
            widget.resize(width, 760)
            fake_art = QPixmap(1200, 420)
            color = Qt.GlobalColor.darkCyan if kind == "fgo-lepton" else Qt.GlobalColor.darkBlue
            fake_art.fill(color)
            widget.set_hero(fake_art)
            widget.show()
            app.processEvents()
            path = outdir / f"{kind}-{width}.png"
            widget.grab().save(str(path))
            widget.close()
            widget.deleteLater()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
