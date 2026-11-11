"""Leftover windows are ended before an app-wide restyle (#1024).

After the GUI test files a worker can hold about 30,000 live widgets (705 top-level windows
measured locally). ``QApplication.setStyleSheet`` repolishes every one of them, and twice on
CI 3.12 that restyle segfaulted (theme.refresh / test_b20_guistyle). The files that restyle the
whole app use the ``restyle_safe`` fixture, which ends earlier tests' windows first.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication, QLabel, QWidget
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _qt_leftovers import end_leftover_windows


def test_leftover_windows_and_their_children_are_gone():
    app = QApplication.instance() or QApplication([])
    windows = []
    for _ in range(3):
        w = QWidget()
        QLabel("child", w)
        w.show()
        windows.append(w)
    before = len(QApplication.allWidgets())
    end_leftover_windows()
    assert all(sip.isdeleted(w) for w in windows)
    assert len(QApplication.allWidgets()) <= before - 6
    app.setStyleSheet("QLabel { color: red; }")  # nothing left to trip over
    app.setStyleSheet("")


def test_the_fixture_runs_before_the_test(restyle_safe):
    assert restyle_safe is QApplication.instance()
    assert not [w for w in QApplication.topLevelWidgets() if not sip.isdeleted(w)]
