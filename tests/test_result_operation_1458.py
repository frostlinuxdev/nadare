# SPDX-License-Identifier: GPL-3.0-or-later
"""#1458: a worker's result carries the operation that produced it.

The window used to guess the operation from the payload's shape (NewsResult -> news, a media
dict -> media, Status -> status, anything else -> Verify), so a new result type or a changed
payload was silently handled as a file check. The controller now says which operation it was.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui import workers
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import operations as operations_mod


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    boxes: list[str] = []
    for kind in ("warning", "information"):
        monkeypatch.setattr(operations_mod.QMessageBox, kind,
                            staticmethod(lambda *a, **k: boxes.append(a[1] if len(a) > 1 else "")))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="direct-bd2", title="Alpha")
    win = MainWindow(config)
    monkeypatch.setattr(win, "_start", lambda *a, **k: True)
    win.boxes = boxes
    win.open_game("alpha")
    yield win
    win.controller._workers.clear()
    win.close()
    win.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize(
    "operation, expected",
    [
        ("verify", {"verified": True, "message": "fine"}),
        ("status", Status("installed")),
    ],
)
def test_the_controller_result_names_its_operation(tmp_path, qapp, monkeypatch, operation,
                                                   expected):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="direct-bd2", title="Alpha")

    class _Backend:
        ctx = None

        def verify(self):
            return {"verified": True, "message": "fine"}

        def status(self):
            return Status("installed")

    monkeypatch.setattr(workers, "create", lambda *a, **k: _Backend())
    controller = workers.BackendController(config)
    got: list[tuple] = []
    controller.result.connect(lambda *args: got.append(args))
    try:
        assert controller.start("alpha", operation)
        end = time.monotonic() + 10
        while not got and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(got) == 1
        assert got[0][:2] == ("alpha", operation)
        assert got[0][2] == expected
    finally:
        controller.shutdown()


def test_a_status_shaped_payload_from_verify_is_not_a_status(window):
    window._on_result("alpha", "status", Status("installed"))
    window._on_result("alpha", "verify", Status("error", message="odd payload"))
    assert window._snapshots["alpha"]["status"].state == "installed"


def test_a_verify_shaped_payload_from_another_operation_is_not_a_file_check(window):
    window._on_result("alpha", "status", Status("installed"))
    window._on_result("alpha", "news", {"verified": False, "message": "not a file check"})
    window._on_result("alpha", "mystery", {"verified": False, "message": "not a file check"})
    assert "alpha" not in window._verify_failures
    assert window.boxes == []
    assert window._snapshots["alpha"]["status"].state == "installed"


def test_a_file_check_is_still_shown(window):
    window._on_result("alpha", "status", Status("installed"))
    window._on_result("alpha", "verify", {"verified": False, "message": "one file is wrong"})
    assert window._verify_failures["alpha"] == "one file is wrong"
    assert window._snapshots["alpha"]["status"].state == "error"


def test_a_stale_result_is_judged_by_its_own_operation(window):
    """A Verify started before the profile changed is dropped even though a newer status
    probe (a different operation) is current."""
    window._on_result("alpha", "status", Status("installed"))
    generation = window._profile_generation("alpha")
    window._operation_generations[("alpha", "verify")] = generation - 1
    window._operation_generations[("alpha", "status")] = generation
    window._on_result("alpha", "verify", {"verified": False, "message": "stale check"})
    assert "alpha" not in window._verify_failures
    assert window.boxes == []


def test_the_payload_guesser_is_gone():
    assert not hasattr(operations_mod.OperationsMixin, "_result_operation")
