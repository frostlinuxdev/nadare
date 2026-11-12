# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: every update says what it downloaded, and warns when it is far above the publisher's."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch
from nadare.core.events import Event

GB = 10**9


def test_a_patched_update_reads_like_the_publishers():
    text, warning = direct.update_receipt(
        "3.7.2", "3.8.0", patch_bytes=11 * GB, whole_bytes=2 * GB, publisher_bytes=13 * GB
    )
    assert "3.7.2 -> 3.8.0" in text and "publisher's own update" in text
    assert warning == ""


def test_a_whole_game_download_where_a_patch_existed_is_flagged():
    text, warning = direct.update_receipt(
        "3.7.2", "3.8.0", patch_bytes=0, whole_bytes=88 * GB, publisher_bytes=13 * GB
    )
    assert "far more" in warning and "Support report" in warning


def test_no_publisher_size_means_no_judgement():
    _, warning = direct.update_receipt("1", "2", patch_bytes=0, whole_bytes=50 * GB,
                                       publisher_bytes=0)
    assert warning == ""


def _backend(tmp_path, monkeypatch, *, downloaded: int, publisher: int):
    ctx = make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    events: list[Event] = []
    ctx.emit = events.append
    backend = direct.BrownDust2Backend(ctx)
    root = backend.install_dir()
    root.mkdir(parents=True)
    (root / "BrownDust II.exe").write_bytes(b"MZ")
    backend._write_version_file("3.7.2", "bd2")
    plan = dd.Plan(
        game_tag="bd2", version="3.8.0", exe="BrownDust II.exe",
        extra={kuropatch.EXTRA_KEY: {"root": "https://cdn.example/", "packs": {"": []}}},
    )
    backend._load_manifest = lambda: plan
    monkeypatch.setattr(
        kuropatch, "prepare",
        lambda *a: kuropatch.PatchPlan(groups=(), deletes=(), publisher_bytes=publisher),
    )
    monkeypatch.setattr(kuropatch, "retired_on_disk", lambda *a: ("old.pak",))
    (root / "old.pak").write_bytes(b"left over")

    def sync(plan, dest, *, emit=None, **kwargs):
        emit(Event("progress", "", data={"stage": "download", "done": downloaded // 2}))
        emit(Event("progress", "", data={"stage": "download", "done": downloaded}))
        return {"downloaded": 1, "skipped": 0}

    monkeypatch.setattr(dd, "sync", sync)
    return backend, events


def test_the_backend_logs_the_receipt_and_removes_leftovers(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, downloaded=5 * GB, publisher=13 * GB)
    backend._sync_direct()
    logs = [e.message for e in events if e.kind == "log"]
    assert any("downloaded 4.7 GiB" in m or "downloaded 5" in m for m in logs), logs
    assert not any("far more" in e.message for e in events)
    assert not (backend.install_dir() / "old.pak").exists()


def test_the_backend_warns_on_an_oversized_update(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, downloaded=88 * GB, publisher=13 * GB)
    backend._sync_direct()
    assert any(e.kind == "step" and "far more" in e.message for e in events)


def test_a_failing_cleanup_never_fails_the_update(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, downloaded=GB, publisher=13 * GB)
    monkeypatch.setattr(
        kuropatch, "retired_on_disk", lambda *a: (_ for _ in ()).throw(OSError("disk"))
    )
    backend._sync_direct()
    assert (backend.install_dir() / "old.pak").exists()
    assert any("could not check for left-over files" in e.message for e in events)


@pytest.mark.parametrize("installed", ["3.8.0"])
def test_a_repair_cleans_up_but_writes_no_update_receipt(tmp_path, monkeypatch, installed):
    backend, events = _backend(tmp_path, monkeypatch, downloaded=GB, publisher=13 * GB)
    backend._write_version_file(installed, "bd2")
    backend._sync_direct()
    assert not any("update 3.8.0" in e.message for e in events)
    assert not (backend.install_dir() / "old.pak").exists()
