# SPDX-License-Identifier: GPL-3.0-or-later
"""conftest turns an unstubbed message box into a failure, never a hang of the whole run."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from conftest import RealModalBox


def test_an_unstubbed_box_raises_instead_of_waiting():
    QApplication.instance() or QApplication([])
    with pytest.raises(RealModalBox):
        QMessageBox.warning(None, "Title", "This box would wait for a click that never comes")
    with pytest.raises(RealModalBox):
        QMessageBox(QMessageBox.Icon.Warning, "T", "a box built by hand and then shown").exec()


def test_a_test_can_still_stub_a_box_itself(monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: "stubbed"))
    assert QMessageBox.warning(None, "T", "x") == "stubbed"
