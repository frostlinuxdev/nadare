# SPDX-License-Identifier: GPL-3.0-or-later
"""The direct backend attaches published archive pins to a streamed plan."""

from __future__ import annotations

import dataclasses

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import archivepins
from nadare.core import directdl as dd
from nadare.core.backends import BackendError

URL = "https://cdn.example/g.zip"
DATA = b"x" * 10_000
BS = 4096


def _plan(size: int = len(DATA)) -> dd.Plan:
    source = dd.RemoteFile("g.zip", URL, size)
    return dd.Plan("T", "1", archives=(dd.Archive("g", source, stream=True),))


def _backend(tmp_path, monkeypatch, plan):
    monkeypatch.setattr(
        direct.BrownDust2Backend, "plan_factory", staticmethod(lambda fetch=None: plan)
    )
    return direct.BrownDust2Backend(make_context(tmp_path, "direct-bd2", {}))


def _pins(monkeypatch, pins, *, available=True, known=True):
    monkeypatch.setattr(
        archivepins,
        "load_state",
        lambda tag: archivepins.Loaded(pins=pins, available=available, known=known),
    )


def test_a_matching_pin_is_attached_to_a_streamed_archive(tmp_path, monkeypatch):
    pin = archivepins.pin_from_bytes(URL, DATA, BS)
    _pins(monkeypatch, {"g": pin})
    backend = _backend(tmp_path, monkeypatch, _plan())
    assert backend.plan().archives[0].pin == pin


def test_a_changed_file_at_the_pinned_url_refuses_before_anything_is_written(
    tmp_path, monkeypatch
):
    """#1158 (was: the pin was dropped silently and the install streamed on)."""
    pin = archivepins.pin_from_bytes(URL, DATA, BS)
    _pins(monkeypatch, {"g": pin})
    backend = _backend(tmp_path, monkeypatch, _plan(size=len(DATA) + 1))
    with pytest.raises(BackendError, match="changed") as info:
        backend.plan()
    assert "report" in str(info.value).lower()
    assert not backend.install_dir().exists() or not any(backend.install_dir().iterdir())


class _StopAfterNotes(Exception):
    pass


def _sync_events(tmp_path, monkeypatch, backend):
    """Run _sync_direct up to the download, which is stopped; return what was emitted."""

    def stop(*_a, **_kw):
        raise _StopAfterNotes

    monkeypatch.setattr(direct.directdl, "sync", stop)
    monkeypatch.setattr(direct.directdl, "plan_workers", lambda plan: 1)
    events = []
    backend.ctx.emit = events.append
    with pytest.raises(_StopAfterNotes):
        backend._sync_direct()
    return events


def test_a_pin_for_another_version_installs_and_says_so_visibly(tmp_path, monkeypatch):
    pin = archivepins.pin_from_bytes("https://cdn.example/old/g.zip", DATA, BS)
    _pins(monkeypatch, {"g": pin})
    backend = _backend(tmp_path, monkeypatch, _plan())
    events = []
    backend.ctx.emit = events.append
    assert backend.plan().archives[0].pin is None
    # Reading the plan (e.g. the background update check) only logs it ...
    assert any("not checked" in e.message.lower() for e in events if e.kind == "log")
    assert not any("not checked" in e.message.lower() for e in events if e.kind == "step")
    # ... an install or update shows it on the page.
    events = _sync_events(tmp_path, monkeypatch, backend)
    assert any("not checked" in e.message.lower() for e in events if e.kind == "step")


def test_a_game_without_pins_installs_quietly(tmp_path, monkeypatch):
    _pins(monkeypatch, {}, available=False, known=False)
    backend = _backend(tmp_path, monkeypatch, _plan())
    events = []
    backend.ctx.emit = events.append
    assert backend.plan().archives[0].pin is None
    assert not any("not checked" in e.message.lower() for e in events)


def test_a_failing_pin_load_leaves_the_plan_unchanged(tmp_path, monkeypatch):
    def boom(tag):
        raise RuntimeError("pins are down")

    monkeypatch.setattr(archivepins, "load_state", boom)
    original = _plan()
    backend = _backend(tmp_path, monkeypatch, original)
    plan = backend.plan()
    assert plan.archives[0].pin is None
    assert dataclasses.replace(plan, archives=()) == dataclasses.replace(original, archives=())
