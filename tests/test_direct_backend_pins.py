# SPDX-License-Identifier: GPL-3.0-or-later
"""The direct backend attaches published archive pins to a streamed plan."""

from __future__ import annotations

import dataclasses

from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import archivepins
from nadare.core import directdl as dd

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


def test_a_matching_pin_is_attached_to_a_streamed_archive(tmp_path, monkeypatch):
    pin = archivepins.pin_from_bytes(URL, DATA, BS)
    monkeypatch.setattr(archivepins, "load", lambda tag: {"g": pin})
    backend = _backend(tmp_path, monkeypatch, _plan())
    assert backend.plan().archives[0].pin == pin


def test_an_unmatched_pin_leaves_the_archive_plain(tmp_path, monkeypatch):
    pin = archivepins.pin_from_bytes(URL, DATA, BS)
    monkeypatch.setattr(archivepins, "load", lambda tag: {"g": pin})
    backend = _backend(tmp_path, monkeypatch, _plan(size=len(DATA) + 1))
    assert backend.plan().archives[0].pin is None


def test_a_failing_pin_load_leaves_the_plan_unchanged(tmp_path, monkeypatch):
    def boom(tag):
        raise RuntimeError("pins are down")

    monkeypatch.setattr(archivepins, "load", boom)
    original = _plan()
    backend = _backend(tmp_path, monkeypatch, original)
    plan = backend.plan()
    assert plan.archives[0].pin is None
    assert dataclasses.replace(plan, archives=()) == dataclasses.replace(original, archives=())
