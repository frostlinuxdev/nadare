# SPDX-License-Identifier: GPL-3.0-or-later
"""Art Qt cannot scale (a null intermediate) must degrade, not spin a worker forever."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": SRC}
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=20
    )


def test_soft_of_a_null_image_returns_instead_of_looping():
    done = _run(
        "from PyQt6.QtGui import QImage, QGuiApplication\n"
        "app = QGuiApplication([])\n"
        "from nadare.gui import images\n"
        "out = images._soft(QImage(), 330, 186)\n"
        "print('ok', out.isNull())\n"
    )
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr[-300:]


def test_the_fit_over_blur_of_a_picture_that_cannot_be_expanded_falls_back():
    done = _run(
        "from PyQt6.QtGui import QImage, QColor, QGuiApplication\n"
        "app = QGuiApplication([])\n"
        "from nadare.gui import images\n"
        "img = QImage(1, 2000, QImage.Format.Format_ARGB32); img.fill(QColor(10, 200, 30))\n"
        "real = QImage.scaled\n"
        "def fail_expand(self, *a, **k):\n"
        "    if len(a) >= 3 and a[2] == images._EXPAND:\n"
        "        return QImage()  # what Qt returns when the buffer cannot be allocated\n"
        "    return real(self, *a, **k)\n"
        "QImage.scaled = fail_expand\n"
        "out = images._fit_over_blur(img, 330, 186)\n"
        "print('ok', out.width(), out.height())\n"
    )
    assert done.returncode == 0 and "ok 330 186" in done.stdout, done.stderr[-300:]
