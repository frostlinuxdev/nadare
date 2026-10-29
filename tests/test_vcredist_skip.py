# SPDX-License-Identifier: GPL-3.0-or-later
"""The VC++ step must never trap a launch: shorter limit, no display, cancel, plain wording.

A tester pressed Cancel a few times on a stuck "Installing the Visual C++ runtime" step and every
relaunch started the installer again. These tests pin the behaviour that breaks that loop.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import vcredist
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

PAYLOAD = b"MZ-fake-vc-redist"


def _prefix(tmp_path) -> Path:
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "windows" / "system32").mkdir(parents=True)
    return prefix


def _install(tmp_path, monkeypatch, run, *, events=None, cancel=None):
    """Run ``vcredist.install`` against a prefilled cache so nothing touches the network."""
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "vc_redist.x64.exe").write_bytes(PAYLOAD)
    vcredist.install(
        _prefix(tmp_path) if not (tmp_path / "pfx").exists() else tmp_path / "pfx",
        profile={},
        cache_dir=cache,
        run_in_prefix=run,
        emit=(events.append if events is not None else (lambda _e: None)),
        cancel=cancel or Cancel(),
        constant=hashlib.sha256(PAYLOAD).hexdigest(),
    )


def _ensure(prefix, install_fn, emitted, **kw):
    vcredist.ensure(
        prefix,
        profile={},
        cache_dir=prefix.parent / "cache",
        run_in_prefix=lambda argv: 0,
        emit=emitted.append,
        cancel=SimpleNamespace(cancelled=False),
        install_fn=install_fn,
        **kw,
    )


# ---- the time limit ---------------------------------------------------------------------
def test_the_limit_is_ninety_seconds(monkeypatch):
    monkeypatch.delenv("NADARE_VCREDIST_TIMEOUT", raising=False)
    assert vcredist._install_timeout() == 90.0


def test_a_timeout_names_seconds_not_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "0.2")
    import threading

    release = threading.Event()
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: release.set() or 1)

    def hang(_argv):
        release.wait(10)
        return 1

    with pytest.raises(BackendError) as err:
        _install(tmp_path, monkeypatch, hang)
    assert "did not finish" in err.value.message
    assert "minute(s)" not in err.value.message
    assert "second" in err.value.message


# ---- no display -------------------------------------------------------------------------
def test_no_display_skips_at_once_and_is_not_remembered(tmp_path):
    prefix = _prefix(tmp_path)
    calls, events = [], []
    _ensure(prefix, lambda: calls.append(1), events, display_fn=lambda: False)
    assert calls == []
    assert any("no screen" in e.message.lower() for e in events)
    assert not (prefix / vcredist.FAILED_MARKER).exists()
    # the next launch from a desktop session does run it
    _ensure(prefix, lambda: calls.append(1), [], display_fn=lambda: True)
    assert calls == [1]


def test_an_installed_runtime_never_asks_for_a_display(tmp_path):
    prefix = _prefix(tmp_path)
    root = prefix / "drive_c" / "windows" / "system32"
    (root / "vcruntime140.dll").write_bytes(b"real")
    (root / "msvcp140.dll").write_bytes(b"real")
    events: list = []
    _ensure(prefix, lambda: pytest.fail("ran"), events, display_fn=lambda: False)
    assert events == []


# ---- cancel while the installer runs ----------------------------------------------------
def test_a_cancel_during_the_installer_skips_the_next_launch(tmp_path, monkeypatch):
    def cancelled(_argv):
        raise Cancelled()

    with pytest.raises(Cancelled):
        _install(tmp_path, monkeypatch, cancelled)
    marker = tmp_path / "pfx" / vcredist.FAILED_MARKER
    assert marker.exists()

    calls, events = [], []
    _ensure(tmp_path / "pfx", lambda: calls.append(1), events)
    assert calls == [], "the relaunch must not start the installer again"
    assert any("cancel" in e.message.lower() for e in events)


def test_the_cancel_wait_is_short_and_then_it_tries_again(tmp_path, monkeypatch):
    def cancelled(_argv):
        raise Cancelled()

    with pytest.raises(Cancelled):
        _install(tmp_path, monkeypatch, cancelled)
    marker = tmp_path / "pfx" / vcredist.FAILED_MARKER
    assert vcredist.RETRY_AFTER_CANCEL_SECONDS < vcredist.RETRY_AFTER_FAILURE_SECONDS
    old = time.time() - vcredist.RETRY_AFTER_CANCEL_SECONDS - 5
    os.utime(marker, (old, old))
    calls: list = []
    _ensure(tmp_path / "pfx", lambda: calls.append(1), [])
    assert calls == [1]


def test_a_cancel_before_the_installer_starts_leaves_no_marker(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    cancel = Cancel()
    cancel.cancel()

    def fake_download(url, tmp, dest):
        raise Cancelled()

    with pytest.raises(Cancelled):
        vcredist.install(
            _prefix(tmp_path),
            profile={},
            cache_dir=cache,
            run_in_prefix=lambda argv: 0,
            emit=lambda _e: None,
            cancel=cancel,
            constant=hashlib.sha256(PAYLOAD).hexdigest(),
            download_fn=fake_download,
        )
    assert not (tmp_path / "pfx" / vcredist.FAILED_MARKER).exists()


def test_a_real_failure_still_waits_a_day(tmp_path):
    prefix = _prefix(tmp_path)
    calls: list = []

    def hang():
        calls.append(1)
        raise BackendError("the Visual C++ installer did not finish within 90 seconds")

    _ensure(prefix, hang, [])
    marker = prefix / vcredist.FAILED_MARKER
    old = time.time() - vcredist.RETRY_AFTER_CANCEL_SECONDS - 5  # past a cancel wait only
    os.utime(marker, (old, old))
    _ensure(prefix, hang, [])
    assert calls == [1]


# ---- wording ----------------------------------------------------------------------------
def test_the_steps_are_plain_and_say_how_long(tmp_path, monkeypatch):
    events: list = []
    _install(tmp_path, monkeypatch, lambda argv: 0, events=events)
    steps = [e.message for e in events if e.kind == "step"]
    assert len(steps) == 2 and steps[0] != steps[1]
    text = " ".join(steps)
    assert "Windows component" in text
    assert "90 seconds" in text


def test_a_failure_says_what_happened_and_that_the_game_still_starts(tmp_path):
    prefix = _prefix(tmp_path)
    events: list = []

    def hang():
        raise BackendError("the Visual C++ installer did not finish within 90 seconds")

    _ensure(prefix, hang, events)
    message = events[-1].message
    assert "skipped" in message
    assert "should still start" in message
    assert "NADARE_" not in message


def test_the_day_long_skip_message_has_no_environment_variable_talk(tmp_path):
    prefix = _prefix(tmp_path)

    def hang():
        raise BackendError("stuck")

    _ensure(prefix, hang, [])
    events: list = []
    _ensure(prefix, hang, events)
    assert events and "NADARE_" not in events[-1].message
    assert "did not finish last time" in events[-1].message
