# SPDX-License-Identifier: GPL-3.0-or-later
"""Skip the Visual C++ step: a user escape that keeps the launch going (unlike Cancel).

The tester's only way out of a stuck VC++ install was Cancel, which aborted the whole launch.
Skip stops just the installer, the game still starts, and the next launches do not ask again
for a while.
"""

from __future__ import annotations

import hashlib
import os
import threading
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
    (prefix / "drive_c" / "windows" / "system32").mkdir(parents=True, exist_ok=True)
    return prefix


def _install(tmp_path, run, *, cancel=None, events=None):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "vc_redist.x64.exe").write_bytes(PAYLOAD)
    vcredist.install(
        _prefix(tmp_path),
        profile={},
        cache_dir=cache,
        run_in_prefix=run,
        emit=(events.append if events is not None else (lambda _e: None)),
        cancel=cancel or Cancel(),
        constant=hashlib.sha256(PAYLOAD).hexdigest(),
    )


def _ensure(prefix, install_fn, emitted):
    vcredist.ensure(
        prefix,
        profile={},
        cache_dir=prefix.parent / "cache",
        run_in_prefix=lambda argv: 0,
        emit=emitted.append,
        cancel=SimpleNamespace(cancelled=False),
        install_fn=install_fn,
    )


# ---- the token ---------------------------------------------------------------------------
def test_skip_is_a_separate_flag_from_cancel():
    token = Cancel()
    assert token.skip_requested is False
    token.skip_step()
    assert token.skip_requested is True
    assert token.cancelled is False
    token.raise_if_cancelled()  # a skip is not a cancel
    token.reset_skip()
    assert token.skip_requested is False


# ---- the step event tells the GUI when Skip makes sense -----------------------------------
def test_only_the_installer_step_is_skippable(tmp_path):
    events: list = []
    _install(tmp_path, lambda argv: 0, events=events)
    steps = [e for e in events if e.kind == "step"]
    assert len(steps) == 2
    assert not (steps[0].data or {}).get("skippable")
    assert (steps[1].data or {}).get("skippable") is True


# ---- skipping stops the installer, not the launch ------------------------------------------
def test_skip_stops_the_installer_and_raises_step_skipped(tmp_path, monkeypatch):
    released = threading.Event()
    stops: list[int] = []

    def stop(*_a, **_k):
        stops.append(1)
        released.set()
        return 1

    monkeypatch.setattr(vcredist, "_stop_installer", stop)
    token = Cancel()

    def hang(_argv):
        released.wait(10)  # only the skip frees it
        return 1

    threading.Timer(0.3, token.skip_step).start()
    start = time.monotonic()
    with pytest.raises(vcredist.StepSkipped):
        _install(tmp_path, hang, cancel=token)
    assert time.monotonic() - start < 5
    assert stops == [1]


def test_step_skipped_is_a_backend_error_so_old_callers_still_catch_it():
    assert issubclass(vcredist.StepSkipped, BackendError)


def test_a_skip_asked_before_the_installer_starts_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vcredist, "_stop_installer", lambda *a, **k: pytest.fail("stopped a healthy installer")
    )
    token = Cancel()
    token.skip_step()  # a stale click from an earlier moment
    ran: list = []
    _install(tmp_path, lambda argv: ran.append(argv) or 0, cancel=token)
    assert ran
    assert token.skip_requested is False


def test_a_cancel_still_beats_a_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: 1)

    def cancelled(_argv):
        raise Cancelled()

    token = Cancel()
    with pytest.raises(Cancelled):
        _install(tmp_path, cancelled, cancel=token)


def test_a_cancel_object_without_skip_support_is_fine(tmp_path):
    ns = SimpleNamespace(cancelled=False)
    ran: list = []
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "vc_redist.x64.exe").write_bytes(PAYLOAD)
    vcredist.install(
        _prefix(tmp_path),
        profile={},
        cache_dir=cache,
        run_in_prefix=lambda argv: ran.append(argv) or 0,
        emit=lambda _e: None,
        cancel=ns,
        constant=hashlib.sha256(PAYLOAD).hexdigest(),
    )
    assert ran


# ---- ensure: the launch goes on, the next launches do not ask again for a while -------------
def test_ensure_carries_on_after_a_skip_and_remembers_it(tmp_path):
    prefix = _prefix(tmp_path)
    calls: list = []
    events: list = []

    def skipped():
        calls.append(1)
        raise vcredist.StepSkipped("the Visual C++ installer was skipped")

    _ensure(prefix, skipped, events)  # must not raise
    assert calls == [1]
    message = events[-1].message
    assert "skipped" in message.lower() and "should still start" in message
    assert (prefix / vcredist.FAILED_MARKER).exists()

    again: list = []
    _ensure(prefix, skipped, again)
    assert calls == [1], "the next launch must not run the installer again"
    assert "you skipped" in again[-1].message.lower()


def test_the_skip_wait_is_the_short_one_and_then_it_tries_again(tmp_path):
    prefix = _prefix(tmp_path)
    calls: list = []

    def skipped():
        calls.append(1)
        raise vcredist.StepSkipped("skipped")

    _ensure(prefix, skipped, [])
    old = time.time() - vcredist.RETRY_AFTER_CANCEL_SECONDS - 5
    os.utime(prefix / vcredist.FAILED_MARKER, (old, old))
    _ensure(prefix, skipped, [])
    assert calls == [1, 1]
