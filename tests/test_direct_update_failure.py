# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed update tells the truth: part-way means interrupted, nothing replaced means untouched."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled


def _backend(tmp_path, *, installed: bool):
    ctx = make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    backend = direct.BrownDust2Backend(ctx)
    root = backend.install_dir()
    root.mkdir(parents=True)
    if installed:
        (root / "BrownDust II.exe").write_bytes(b"MZ")
        backend._write_version_file("1.0", "bd2")
    plan = dd.Plan(game_tag="bd2", version="2.0", exe="BrownDust II.exe")
    backend._load_manifest = lambda: plan
    return backend


def _sync_that(monkeypatch, *, progress: bool, error: BaseException):
    """``progress`` means a file was already replaced when the sync failed."""

    def fake(plan, dest, *, emit=None, cancel=None, workers=4, check_workers=None, changed=None):
        if progress:
            changed.set()
        raise error

    monkeypatch.setattr(dd, "sync", fake)


def test_an_update_that_fails_before_replacing_anything_leaves_the_game_playable(
    tmp_path, monkeypatch
):
    backend = _backend(tmp_path, installed=True)
    _sync_that(monkeypatch, progress=False, error=dd.DirectError("could not reach the CDN"))
    with pytest.raises(BackendError, match="Nothing was left half-installed"):
        backend._sync_direct()
    assert not backend._install_incomplete()


def test_an_update_that_fails_after_replacing_a_file_stays_marked_interrupted(
    tmp_path, monkeypatch
):
    backend = _backend(tmp_path, installed=True)
    _sync_that(monkeypatch, progress=True, error=dd.DirectError("disk full"))
    with pytest.raises(BackendError) as caught:
        backend._sync_direct()
    assert backend._install_incomplete()
    text = str(caught.value)
    assert "half-installed" not in text.replace("may be half-installed", "")
    assert "part-way" in text and ("RESUME" in text or "Repair" in text)


def test_a_cancelled_update_after_replacing_a_file_stays_marked_interrupted(tmp_path, monkeypatch):
    backend = _backend(tmp_path, installed=True)
    _sync_that(monkeypatch, progress=True, error=Cancelled())
    with pytest.raises(Cancelled):
        backend._sync_direct()
    assert backend._install_incomplete()


def test_a_cancelled_update_before_replacing_anything_leaves_the_game_playable(
    tmp_path, monkeypatch
):
    backend = _backend(tmp_path, installed=True)
    _sync_that(monkeypatch, progress=False, error=Cancelled())
    with pytest.raises(Cancelled):
        backend._sync_direct()
    assert not backend._install_incomplete()


def test_a_failed_fresh_install_stays_incomplete_as_before(tmp_path, monkeypatch):
    backend = _backend(tmp_path, installed=False)
    _sync_that(monkeypatch, progress=False, error=dd.DirectError("could not reach the CDN"))
    with pytest.raises(BackendError):
        backend._sync_direct()
    assert backend._install_incomplete()


def test_a_finished_update_clears_the_marker(tmp_path, monkeypatch):
    backend = _backend(tmp_path, installed=True)
    monkeypatch.setattr(dd, "sync", lambda *a, **k: {"downloaded": 1, "skipped": 0})
    backend._sync_direct()
    assert not backend._install_incomplete()


def test_the_real_sync_sets_changed_once_a_file_is_replaced(tmp_path, monkeypatch):
    seen = {}

    def fake_download(rf, dest, *, emit=None, cancel=None, pool=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    files = (dd.RemoteFile("a.bin", "https://example.invalid/a", 1),)
    plan = dd.Plan("T", "1", files=files)
    monkeypatch.setattr(dd, "plan_missing", lambda *a, **k: (list(files), []))
    import threading

    changed = threading.Event()
    dd.sync(plan, tmp_path / "game", workers=1, changed=changed)
    seen["after"] = changed.is_set()
    assert seen["after"]


def test_the_real_sync_leaves_changed_clear_when_nothing_was_replaced(tmp_path, monkeypatch):
    import threading

    def boom(rf, dest, *, emit=None, cancel=None, pool=None):
        raise dd.DirectError("HTTP 500")

    monkeypatch.setattr(dd, "_download_remote", boom)
    files = (dd.RemoteFile("a.bin", "https://example.invalid/a", 1),)
    plan = dd.Plan("T", "1", files=files)
    monkeypatch.setattr(dd, "plan_missing", lambda *a, **k: (list(files), []))
    changed = threading.Event()
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path / "game", workers=1, changed=changed)
    assert not changed.is_set()
