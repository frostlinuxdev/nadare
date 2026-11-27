# SPDX-License-Identifier: GPL-3.0-or-later
"""A game that is not ready is not offered for new installs (b27: Strinova, #1819, #1905).

Strinova starts through its own launcher (#1819), but that launcher patches the game behind
Nadare's back and its download server often does not answer, so it is held back again (#1905).
The fixture tests hold another kind back on purpose (``direct-pgr``) to check the mechanism.
The kind of a held-back game stays loadable, so an existing profile still opens.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nadare.core import backends, storecatalog
from nadare.core import directkinds as dk
from nadare.gui import add_game

ROOT = Path(__file__).resolve().parents[1]
HELD = frozenset({"direct-pgr"})


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setattr(dk, "HELD_BACK_KINDS", HELD)
    monkeypatch.setattr(storecatalog, "HELD_BACK_KINDS", HELD)
    monkeypatch.setattr(add_game, "HELD_BACK_KINDS", HELD)
    return HELD


@pytest.fixture
def pi():
    spec = importlib.util.spec_from_file_location(
        "publish_index_held_back", ROOT / "scripts" / "publish_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_only_strinova_is_held_back_in_this_build():
    assert dk.HELD_BACK_KINDS == frozenset({"direct-strinova"})


def test_strinova_is_not_offered():
    assert storecatalog.entries(query="strinova") == []
    assert "direct-strinova" not in {g.kind for g in add_game.available_catalog()}


def test_strinova_is_not_reported_as_broken():
    assert "direct-strinova" not in {kind for kind, _t in storecatalog.unavailable_games()}


def test_strinova_is_not_published(pi):
    assert "strinova" not in set(pi.default_catalog_games())


def test_an_existing_strinova_profile_still_loads():
    assert "direct-strinova" in dk.DIRECT_KINDS
    assert backends._load_builtin("direct-strinova") is not None


def test_held_back_kinds_are_not_in_the_store(held):
    shown = {entry.kind for entry in storecatalog.entries()}
    assert not (held & shown)
    assert {"direct-bd2", "direct-wuwa"} <= shown
    assert storecatalog.entries(query="punishing") == []


def test_held_back_kinds_are_not_reported_as_broken(held):
    unavailable = {kind for kind, _title in storecatalog.unavailable_games()}
    assert not (held & unavailable)


def test_held_back_kinds_are_not_in_the_add_game_dialog(held):
    shown = {g.kind for g in add_game.available_catalog()}
    assert not (held & shown)
    assert {"direct-bd2", "direct-wuwa"} <= shown


def test_held_back_kinds_are_not_published(held, pi):
    published = set(pi.default_catalog_games())
    assert "pgr" not in published
    assert "wuwa" in published


def test_a_held_back_kind_still_loads(held):
    assert "direct-pgr" in dk.DIRECT_KINDS
    assert backends._load_builtin("direct-pgr") is not None
