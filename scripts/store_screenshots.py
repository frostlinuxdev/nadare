#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Render reviewer screenshots of the store at three widths without network art."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork
from nadare.gui.store import StorePage
from nadare.gui.theme import apply_theme


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", type=Path)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_Use96Dpi, True)
    app = QApplication.instance() or QApplication([])
    apply_theme(app)

    def fake_fetch(profile, title=None, kind="cover"):
        name = str(title or profile.get("backend") or "game")
        digest = hashlib.sha256(name.encode()).digest()
        first = QColor(35 + digest[0] % 75, 45 + digest[1] % 85, 95 + digest[2] % 100)
        second = first.darker(220)
        image = QImage(600, 900, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        gradient = QLinearGradient(0, 0, 600, 900)
        gradient.setColorAt(0, first)
        gradient.setColorAt(1, second)
        painter.fillRect(image.rect(), gradient)
        painter.setPen(QColor(255, 255, 255, 105))
        painter.setFont(QFont("sans-serif", 140, QFont.Weight.Bold))
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, name[:1].upper())
        painter.end()
        fake_art = args.outdir / f"fake-{hashlib.sha256(name.encode()).hexdigest()[:12]}.png"
        image.save(str(fake_art))
        return fake_art

    artwork.fetch = fake_fetch

    outputs = []
    for width in (520, 900, 1400):
        for search in (None, "gray"):
            page = StorePage()
            page.resize(width, 860)
            if search:
                page.search.setText(search)
            page.show()
            app.processEvents()
            page.wait_for_images()
            app.processEvents()
            suffix = "search" if search else "default"
            output = args.outdir / f"store-{width}-{suffix}.png"
            if not page.grab().save(str(output)):
                raise RuntimeError(f"could not save screenshot: {output}")
            outputs.append(output)
            page.close()
            page.deleteLater()
            app.processEvents()

    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
