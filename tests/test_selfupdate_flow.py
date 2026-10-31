# SPDX-License-Identifier: GPL-3.0-or-later
"""#771: the self-update flow runs on a worker thread and reports one clear outcome."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import selfupdate
from nadare.core.download import IntegrityError
from nadare.core.events import Cancelled, Event
from nadare.core.updatecheck import UpdateInfo
from nadare.gui.selfupdate_flow import SelfUpdateFlow

INFO = UpdateInfo(
    version="0.1.0b20",
    url="https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
    name="Nadare 0.1.0b20",
)
NAME = "Nadare-0.1.0b20-x86_64.AppImage"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _steps(tmp_path: Path, **overrides):
    """A stand-in for core.selfupdate: a target in tmp_path and recording fakes."""
    target = tmp_path / "Nadare-0.1.0b19-x86_64.AppImage"
    target.write_bytes(b"old")
    calls: list[str] = []

    def download_verified(url, dest, sha256, *, emit=None, cancel=None, **kw):
        calls.append("download")
        dest.write_bytes(b"new-bytes")
        if emit is not None:
            data = {"stage": "download", "done": 5, "total": 10}
            emit(Event("progress", "x", progress=0.5, data=data))

    def install_update(new, target_, filename):
        calls.append("install")
        return selfupdate.install_update(new, target_, filename)

    steps = SimpleNamespace(
        asset_urls=selfupdate.asset_urls,
        appimage_target=lambda: target,
        expected_sha256=lambda info: SimpleNamespace(sha256="a" * 64, source="index"),
        download_verified=download_verified,
        install_update=install_update,
        SelfUpdateError=selfupdate.SelfUpdateError,
    )
    for key, value in overrides.items():
        setattr(steps, key, value)
    steps.calls = calls
    steps.target = target
    return steps


def _collect(flow):
    seen: dict[str, list] = {"installed": [], "failed": [], "cancelled": [], "progress": []}
    flow.installed.connect(seen["installed"].append)
    flow.failed.connect(seen["failed"].append)
    flow.cancelled.connect(lambda: seen["cancelled"].append(True))
    flow.progress.connect(lambda done, total: seen["progress"].append((done, total)))
    return seen


def test_can_update_needs_a_github_release_and_a_writable_appimage(tmp_path, qapp):
    flow = SelfUpdateFlow(steps=_steps(tmp_path))
    assert flow.can_update(INFO)
    assert not flow.can_update(UpdateInfo("0.1.0b20", "https://example.org/x", "n"))
    no_target = SelfUpdateFlow(steps=_steps(tmp_path, appimage_target=lambda: None))
    assert not no_target.can_update(INFO)


def test_a_successful_update_installs_off_the_gui_thread(tmp_path, qapp):
    steps = _steps(tmp_path)
    threads: list[threading.Thread] = []
    original = steps.download_verified

    def traced(*args, **kwargs):
        threads.append(threading.current_thread())
        return original(*args, **kwargs)

    steps.download_verified = traced
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    assert flow.start(INFO)
    assert flow.running
    assert _wait(qapp, lambda: bool(seen["installed"]))
    assert threads and threads[0] is not threading.main_thread()
    (new,) = seen["installed"]
    assert new == tmp_path / NAME and new.read_bytes() == b"new-bytes"
    assert (tmp_path / (steps.target.name + ".previous")).read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.download"))
    assert (5, 10) in seen["progress"]
    assert _wait(qapp, lambda: not flow.running)
    assert steps.calls == ["download", "install"]


def test_a_failed_download_installs_nothing_and_leaves_no_temp_file(tmp_path, qapp):
    def boom(url, dest, sha256, **kwargs):
        dest.write_bytes(b"half")
        raise IntegrityError("hash mismatch")

    steps = _steps(tmp_path, download_verified=boom)
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    flow.start(INFO)
    assert _wait(qapp, lambda: bool(seen["failed"]))
    assert "hash mismatch" in seen["failed"][0]
    assert steps.target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.download"))
    assert not seen["installed"]
    assert _wait(qapp, lambda: not flow.running)


def test_a_refused_hash_stops_before_any_download(tmp_path, qapp):
    def refuse(info):
        raise selfupdate.SelfUpdateError("the release checksums disagree with the Nadare index")

    steps = _steps(tmp_path, expected_sha256=refuse)
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    flow.start(INFO)
    assert _wait(qapp, lambda: bool(seen["failed"]))
    assert "disagree" in seen["failed"][0]
    assert steps.calls == []


def test_cancelling_reports_a_cancel_not_an_error(tmp_path, qapp):
    started = threading.Event()

    def slow(url, dest, sha256, *, emit=None, cancel=None, **kw):
        dest.write_bytes(b"half")
        started.set()
        while not cancel.cancelled:
            time.sleep(0.01)
        raise Cancelled("cancelled")

    steps = _steps(tmp_path, download_verified=slow)
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    flow.start(INFO)
    assert started.wait(5)
    flow.cancel()
    assert _wait(qapp, lambda: bool(seen["cancelled"]))
    assert not seen["failed"] and not seen["installed"]
    assert steps.target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.download"))


def test_a_cancel_that_lands_as_the_download_ends_still_stops_before_installing(tmp_path, qapp):
    def finishes_then_cancelled(url, dest, sha256, *, cancel=None, **kw):
        dest.write_bytes(b"new-bytes")
        cancel.cancel()  # the user pressed Cancel just as the last byte arrived

    steps = _steps(tmp_path, download_verified=finishes_then_cancelled)
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    flow.start(INFO)
    assert _wait(qapp, lambda: bool(seen["cancelled"]))
    assert "install" not in steps.calls
    assert steps.target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.download"))


def test_a_second_start_while_running_is_refused(tmp_path, qapp):
    release = threading.Event()

    def slow(url, dest, sha256, **kw):
        release.wait(5)
        dest.write_bytes(b"new-bytes")

    steps = _steps(tmp_path, download_verified=slow)
    flow = SelfUpdateFlow(steps=steps)
    seen = _collect(flow)
    assert flow.start(INFO)
    assert flow.start(INFO) is False
    release.set()
    assert _wait(qapp, lambda: bool(seen["installed"]))
    assert steps.calls.count("install") == 1


def test_an_unexpected_error_is_reported_not_lost(tmp_path, qapp):
    def odd(url, dest, sha256, **kw):
        raise RuntimeError("something odd")

    flow = SelfUpdateFlow(steps=_steps(tmp_path, download_verified=odd))
    seen = _collect(flow)
    flow.start(INFO)
    assert _wait(qapp, lambda: bool(seen["failed"]))
    assert seen["failed"][0]


def teardown_module(module):
    QThreadPool.globalInstance().waitForDone(2000)
