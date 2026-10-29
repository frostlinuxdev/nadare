# SPDX-License-Identifier: GPL-3.0-or-later
"""When the VC++ installer hangs, is skipped or fails, Nadare unpacks the DLLs instead.

Never after a Cancel (the user wants out), and only from the verified private copy.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import vcredist, vcunpack
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

PAYLOAD = b"MZ-fake-vc-redist"


@pytest.fixture(autouse=True)
def _installer_first(monkeypatch):
    """These tests are about the fallback after the installer; the order is covered elsewhere."""
    monkeypatch.setenv("NADARE_VCREDIST_METHOD", "installer")


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


@pytest.fixture
def unpacked(monkeypatch):
    """Record unpack calls; each call sees the private copy that was just verified."""
    calls: list[tuple[Path, bool, bytes]] = []

    def fake(installer, prefix, **_k):
        calls.append((Path(installer), Path(installer).is_file(), Path(installer).read_bytes()))
        return ["vcruntime140.dll", "msvcp140.dll"]

    monkeypatch.setattr(vcredist.vcunpack, "unpack", fake)
    return calls


def _hang_until_stopped(monkeypatch):
    released = threading.Event()
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: released.set() or 1)

    def hang(_argv):
        released.wait(10)
        return 1

    return hang


def test_a_timeout_unpacks_instead_of_failing(tmp_path, monkeypatch, unpacked):
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "0.2")
    events: list = []
    _install(tmp_path, _hang_until_stopped(monkeypatch), events=events)  # must not raise
    assert len(unpacked) == 1
    assert unpacked[0][1] and unpacked[0][2] == PAYLOAD, "unpacks the verified private copy"
    assert any("unpack" in e.message.lower() for e in events if e.kind in ("status", "step"))


def test_a_skip_unpacks_instead_of_leaving_the_runtime_out(tmp_path, monkeypatch, unpacked):
    token = Cancel()
    threading.Timer(0.3, token.skip_step).start()
    _install(tmp_path, _hang_until_stopped(monkeypatch), cancel=token)
    assert len(unpacked) == 1


def test_a_bad_exit_code_unpacks_too(tmp_path, unpacked):
    _install(tmp_path, lambda argv: 5)
    assert len(unpacked) == 1


def test_a_good_run_does_not_unpack(tmp_path, unpacked):
    _install(tmp_path, lambda argv: 0)
    assert unpacked == []


def test_a_cancel_never_unpacks(tmp_path, unpacked):
    def cancelled(_argv):
        raise Cancelled()

    with pytest.raises(Cancelled):
        _install(tmp_path, cancelled)
    assert unpacked == []


def test_when_unpacking_fails_the_original_outcome_stands(tmp_path, monkeypatch):
    def broken(*_a, **_k):
        raise vcunpack.UnpackError("no 7-Zip")

    monkeypatch.setattr(vcredist.vcunpack, "unpack", broken)
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "0.2")
    with pytest.raises(BackendError, match="did not finish"):
        _install(tmp_path, _hang_until_stopped(monkeypatch))

    monkeypatch.delenv("NADARE_VCREDIST_TIMEOUT")
    token = Cancel()
    threading.Timer(0.3, token.skip_step).start()
    with pytest.raises(vcredist.StepSkipped):
        _install(tmp_path, _hang_until_stopped(monkeypatch), cancel=token)

    with pytest.raises(BackendError, match="exited 5"):
        _install(tmp_path, lambda argv: 5)


def test_an_unexpected_unpack_crash_does_not_hide_the_outcome(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(vcredist.vcunpack, "unpack", boom)
    with pytest.raises(BackendError, match="exited 5"):
        _install(tmp_path, lambda argv: 5)


def test_ensure_treats_an_unpack_as_success_and_clears_the_marker(tmp_path, unpacked):
    prefix = _prefix(tmp_path)
    (prefix / vcredist.FAILED_MARKER).write_text("x")
    import os
    import time

    os.utime(prefix / vcredist.FAILED_MARKER, (time.time() - 10**6, time.time() - 10**6))
    events: list = []
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "vc_redist.x64.exe").write_bytes(PAYLOAD)
    vcredist.ensure(
        prefix,
        profile={},
        cache_dir=cache,
        run_in_prefix=lambda argv: 5,
        emit=events.append,
        cancel=SimpleNamespace(cancelled=False),
        constant=hashlib.sha256(PAYLOAD).hexdigest(),
    )
    assert len(unpacked) == 1
    assert not (prefix / vcredist.FAILED_MARKER).exists()
    assert not any("skipped" in e.message.lower() or "could not" in e.message.lower()
                   for e in events if e.kind == "status")
