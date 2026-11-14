# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers and fixtures of the tests split out of test_gui_smoke.py."""

from __future__ import annotations

import os

import pytest

from nadare.core.config import Config

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _empty_config(tmp_path):
    return Config.load(tmp_path / "config.json")


def _wait_for(qapp, predicate, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _collect_garbage_between_tests():
    """Run Python's cyclic GC at a safe point: after the test closed its windows (#1248).

    These tests leave Qt wrappers in reference cycles. When the GC happened to collect them in the
    middle of a later test (it did, inside an import, once the file was split into modules and
    collection allocated more), the process segfaulted; the original single file only got lucky.
    Deferred deletes first, then the collection, so it never runs under a live Qt call."""
    yield
    import gc

    from PyQt6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
