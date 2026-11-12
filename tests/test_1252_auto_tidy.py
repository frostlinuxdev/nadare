# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: b22 players do nothing; b23 removes the retired files on its own, once, safely."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch, process

EXTRA = {kuropatch.EXTRA_KEY: {"root": "https://cdn.example/", "packs": {"": []}}}


@pytest.fixture()
def wuwa(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "pfx")})
    backend = direct.WutheringWavesBackend(ctx)
    root = backend.install_dir()
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "dead.pak").write_bytes(b"retired")
    backend._write_version_file("3.8.0", "wuwa")
    plan = dd.Plan(game_tag="wuwa", version="3.8.0", extra=EXTRA)
    backend._load_manifest = lambda: plan
    calls = []
    monkeypatch.setattr(
        kuropatch, "retired_on_disk", lambda *a: calls.append("scan") or ("Client/dead.pak",)
    )
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    return backend, root, calls


def test_the_update_check_removes_retired_files_once(wuwa):
    backend, root, calls = wuwa
    backend.check_update()
    assert not (root / "Client" / "dead.pak").exists()
    (root / "Client" / "dead.pak").write_bytes(b"again")
    backend.check_update()  # same version: not scanned again
    assert calls == ["scan"]
    assert (root / "Client" / "dead.pak").exists()


def test_not_while_the_game_runs(wuwa, monkeypatch):
    backend, root, calls = wuwa
    monkeypatch.setattr(backend, "_game_running", lambda: True)
    backend.check_update()
    assert (root / "Client" / "dead.pak").exists() and calls == []


def test_not_during_an_unfinished_update(wuwa):
    backend, root, calls = wuwa
    backend._write_install_marker(("3.7.2", "3.8.0"))
    backend.check_update()
    assert (root / "Client" / "dead.pak").exists() and calls == []


def test_not_when_an_update_is_waiting(wuwa):
    backend, root, calls = wuwa
    backend._load_manifest = lambda: dd.Plan(game_tag="wuwa", version="3.9.0", extra=EXTRA)
    backend.check_update()
    assert (root / "Client" / "dead.pak").exists() and calls == []


def test_not_while_another_operation_holds_the_game(wuwa):
    backend, root, calls = wuwa
    lock = process.OperationLock(backend.ctx.name)
    lock.acquire()
    try:
        backend.check_update()
    finally:
        lock.release()
    assert (root / "Client" / "dead.pak").exists() and calls == []


def test_a_failing_tidy_never_breaks_the_check(wuwa, monkeypatch):
    backend, root, calls = wuwa
    monkeypatch.setattr(kuropatch, "retired_on_disk", lambda *a: (_ for _ in ()).throw(OSError()))
    backend.check_update()  # no exception
    assert (root / "Client" / "dead.pak").exists()


def test_other_games_never_tidy(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    backend = direct.BrownDust2Backend(ctx)
    backend.install_dir().mkdir(parents=True)
    backend._write_version_file("2.0", "bd2")
    backend._load_manifest = lambda: dd.Plan(game_tag="bd2", version="2.0", extra=EXTRA)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(kuropatch, "retired_on_disk", lambda *a: pytest.fail("scanned"))
    backend._tidy_retired_once()
