# SPDX-License-Identifier: GPL-3.0-or-later
"""Repair gives the Visual C++ step another chance: a remembered skip or hang is forgotten."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from nadare.core import vcredist


def _prefix(tmp_path) -> Path:
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "windows" / "system32").mkdir(parents=True)
    return prefix


def test_forget_failure_removes_the_marker_and_says_so(tmp_path):
    prefix = _prefix(tmp_path)
    vcredist._mark_failed(prefix, kind="skipped")
    assert vcredist._skip_state(prefix) is not None
    assert vcredist.forget_failure(prefix) is True
    assert not (prefix / vcredist.FAILED_MARKER).exists()
    assert vcredist._skip_state(prefix) is None


def test_forget_failure_is_a_no_op_without_a_marker(tmp_path):
    prefix = _prefix(tmp_path)
    (prefix / "keep.txt").write_text("x")
    assert vcredist.forget_failure(prefix) is False
    assert (prefix / "keep.txt").exists()


def test_forget_failure_on_a_missing_prefix_is_quiet(tmp_path):
    assert vcredist.forget_failure(tmp_path / "nope") is False


# ---- the worker's repair path ---------------------------------------------------------------
try:  # pragma: no cover - exercised only when PyQt6 is missing
    from nadare.core.config import Config
    from nadare.gui.workers import BackendWorker
except Exception:  # noqa: BLE001
    BackendWorker = None  # type: ignore[assignment]


@pytest.fixture
def worker(qtbot, tmp_path):
    if BackendWorker is None:
        pytest.skip("PyQt6 is not importable in this interpreter")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    w = BackendWorker(config, "p", "repair")
    w.events = []
    w.backend_event.connect(w.events.append)
    return w


class _Backend:
    def __init__(self, prefix, name="prefix"):
        self._p = prefix
        setattr(self, name, lambda: self._p)


@pytest.mark.parametrize("accessor", ["prefix", "_prefix"])
def test_repair_forgets_the_marker_and_tells_the_user(worker, tmp_path, accessor):
    prefix = _prefix(tmp_path)
    vcredist._mark_failed(prefix, kind="cancelled")
    worker._retry_runtime_step(_Backend(prefix, accessor))
    assert not (prefix / vcredist.FAILED_MARKER).exists()
    messages = [e.message for e in worker.events if e.kind == "status"]
    assert len(messages) == 1
    assert "Visual C++" in messages[0] and "next launch" in messages[0]


def test_repair_stays_quiet_when_nothing_was_remembered(worker, tmp_path):
    prefix = _prefix(tmp_path)
    worker._retry_runtime_step(_Backend(prefix))
    assert worker.events == []


def test_a_backend_without_a_prefix_is_fine(worker):
    worker._retry_runtime_step(object())
    assert worker.events == []


def test_a_prefix_lookup_that_fails_does_not_break_the_repair(worker):
    class Broken:
        def prefix(self):
            raise RuntimeError("no prefix configured")

    worker._retry_runtime_step(Broken())
    assert worker.events == []
