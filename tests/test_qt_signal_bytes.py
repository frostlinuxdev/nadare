# SPDX-License-Identifier: GPL-3.0-or-later
"""A Qt ``int`` signal argument is 32-bit: 5 GiB arrived as 1 GiB in the found-copies chooser
(#1300). Every signal that carries a byte count must take ``object`` (a Python int)."""

from __future__ import annotations

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001 - PyQt6 may be missing
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

BIG = 6 * 1024**3 + 7  # more than 2**32: wraps in a 32-bit int


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _roundtrip(signal, *args):
    got = []
    signal.connect(lambda *a: got.append(a))
    signal.emit(*args)
    return got[0]


def _args(signal) -> list[str]:
    # "int,int)" or "name(int,int)" depending on whether the signal was used yet
    return signal.signatures[0].rstrip(")").split("(")[-1].split(",")


def test_runner_cleanup_freed_bytes():
    """Deleting four 1.5 GiB runners reported a wrapped "freed" figure."""
    from nadare.gui.main_window import MainWindow

    assert _args(MainWindow.runner_cleanup_done)[1] == "PyQt_PyObject"


def test_runners_dialog_size(qapp):
    from nadare.gui.runners_dialog import _Worker

    assert _roundtrip(_Worker().size_ready, "x", BIG) == ("x", BIG)


def test_selfupdate_progress(qapp):
    from nadare.gui import selfupdate_flow

    assert _roundtrip(selfupdate_flow._Signals().progress, BIG, BIG) == (BIG, BIG)
    assert _args(selfupdate_flow.SelfUpdateFlow.progress) == ["PyQt_PyObject", "PyQt_PyObject"]


def test_found_copies_size(qapp):
    from nadare.gui.found_copies import FoundCopiesDialog

    assert _args(FoundCopiesDialog.size_measured)[1] == "PyQt_PyObject"
