#!/usr/bin/env python3
"""Render the S4 store review screens using local fake artwork."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork
from nadare.core.config import Config
from nadare.core.storecatalog import entries
from nadare.gui.main_window import MainWindow


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    temp = TemporaryDirectory()
    def fake_fetch(_backend, _title, _size):
        image = QImage(900, 320, QImage.Format.Format_RGB32)
        image.fill(QColor("#366a89"))
        path = Path(temp.name) / f"{_size}.png"
        image.save(str(path))
        return str(path)
    artwork.fetch = fake_fetch
    cfg = Config.load(Path(temp.name) / "config.json")
    window = MainWindow(cfg)
    window.resize(1100, 720)
    window.show()
    app.processEvents()
    window.show_store()
    window.store.grid.show()
    app.processEvents()
    window.grab().save(str(out / "store-grid.png"))
    verified = next(e for e in entries() if e.tier == "Verified" and e.has_art)
    window.store.show_detail(verified.kind)
    app.processEvents()
    window.grab().save(str(out / "store-detail-verified.png"))
    cfg.add_profile("review-added", backend=verified.kind, title=verified.title)
    window.refresh_library()
    window.store.show_detail(verified.kind)
    app.processEvents()
    window.grab().save(str(out / "store-detail-added.png"))
    window.show_library()
    app.processEvents()
    window.grab().save(str(out / "library-added.png"))
    window.close()
    temp.cleanup()


if __name__ == "__main__":
    main()
