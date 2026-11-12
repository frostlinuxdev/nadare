# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252 in the update flow: patches first, then the usual whole-file sync, then retired files."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch
from nadare.core.events import Cancelled

GROUP = kuropatch.Group(dd.RemoteFile("g.krpdiff", "https://x/g", 1, "0" * 32), (), ())
EXTRA = {kuropatch.EXTRA_KEY: {"root": "https://cdn.example/", "packs": {"": []}}}


def _backend(tmp_path, *, installed="3.7.2", version="3.8.0", extra=EXTRA):
    ctx = make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    backend = direct.BrownDust2Backend(ctx)
    root = backend.install_dir()
    root.mkdir(parents=True)
    (root / "BrownDust II.exe").write_bytes(b"MZ")
    (root / "old.pak").write_bytes(b"retired")
    if installed:
        backend._write_version_file(installed, "bd2")
    plan = dd.Plan(game_tag="bd2", version=version, exe="BrownDust II.exe", extra=extra)
    backend._load_manifest = lambda: plan
    return backend


@pytest.fixture()
def calls(monkeypatch):
    seen: list = []

    def prepare(plan, installed, fetch):
        seen.append(("prepare", installed))
        return kuropatch.PatchPlan(groups=(), deletes=("old.pak",))

    def sync(plan, dest, **kwargs):
        seen.append(("sync", (dest / "old.pak").exists()))
        return {"downloaded": 0, "skipped": 1}

    monkeypatch.setattr(kuropatch, "prepare", prepare)
    monkeypatch.setattr(dd, "sync", sync)
    return seen


def test_an_update_asks_for_patches_then_syncs_then_removes_retired_files(tmp_path, calls):
    backend = _backend(tmp_path)
    backend._sync_direct()
    assert calls == [("prepare", "3.7.2"), ("sync", True)]  # retired file still there during sync
    assert not (backend.install_dir() / "old.pak").exists()


def test_a_fresh_install_does_not_look_for_patches(tmp_path, calls):
    backend = _backend(tmp_path, installed="")
    backend._sync_direct()
    assert calls == [("sync", True)]
    assert (backend.install_dir() / "old.pak").exists()


def test_the_same_version_does_not_look_for_patches(tmp_path, calls):
    backend = _backend(tmp_path, installed="3.8.0")
    backend._sync_direct()
    assert [c[0] for c in calls] == ["sync"]


def test_a_game_without_publisher_patches_is_unchanged(tmp_path, calls):
    backend = _backend(tmp_path, extra={})
    backend._sync_direct()
    assert [c[0] for c in calls] == ["sync"]
    assert (backend.install_dir() / "old.pak").exists()


def test_a_broken_patch_step_still_syncs_whole_files(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(
        kuropatch, "prepare", lambda *a: (_ for _ in ()).throw(RuntimeError("surprise"))
    )
    backend = _backend(tmp_path)
    backend._sync_direct()
    assert calls == [("sync", True)]
    assert (backend.install_dir() / "old.pak").exists()  # nothing retired without a patch list


def test_a_failed_sync_retires_nothing(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(dd, "sync", lambda *a, **k: (_ for _ in ()).throw(dd.DirectError("net")))
    backend = _backend(tmp_path)
    with pytest.raises(Exception):  # noqa: B017 - BackendError from the sync
        backend._sync_direct()
    assert (backend.install_dir() / "old.pak").exists()


def test_cancel_during_patching_stops_the_update(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(
        kuropatch, "prepare",
        lambda *a: kuropatch.PatchPlan(groups=(GROUP,), deletes=()),
    )
    monkeypatch.setattr(kuropatch, "apply", lambda *a, **k: (_ for _ in ()).throw(Cancelled()))
    backend = _backend(tmp_path)
    with pytest.raises(Cancelled):
        backend._sync_direct()
    assert calls == []


def test_cancel_before_any_file_changed_leaves_the_game_playable(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(
        kuropatch, "prepare",
        lambda *a: kuropatch.PatchPlan(groups=(GROUP,), deletes=()),
    )
    monkeypatch.setattr(kuropatch, "apply", lambda *a, **k: (_ for _ in ()).throw(Cancelled()))
    backend = _backend(tmp_path)
    with pytest.raises(Cancelled):
        backend._sync_direct()
    assert not backend._install_incomplete()
