# SPDX-License-Identifier: GPL-3.0-or-later
"""#624: the library keeps its cards when nothing they show has changed.

``refresh`` deleted and recreated every card (and re-decoded every cover) on each call, which a
Settings save or a profile change in another process triggers even when no card changes.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.gui.library import LibraryPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _page(qapp):
    page = LibraryPage()
    return page


def test_the_same_profiles_keep_the_same_cards(qapp):
    page = _page(qapp)
    profiles = {"a": {"title": "A", "backend": "wine"}, "b": {"title": "B", "backend": "wine"}}
    try:
        page.refresh(profiles)
        before = dict(page._cards)
        page.set_state("a", "installed")
        page.refresh({k: dict(v) for k, v in profiles.items()})  # an equal copy
        assert page._cards == before
        assert all(page._cards[k] is before[k] for k in before)
    finally:
        page.deleteLater()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["a"].update(title="A2"),
        lambda p: p["a"].update(backend="steam"),
        lambda p: p.update(c={"title": "C", "backend": "wine"}),
        lambda p: p.pop("b"),
    ],
)
def test_a_visible_change_rebuilds(qapp, change):
    page = _page(qapp)
    profiles = {"a": {"title": "A", "backend": "wine"}, "b": {"title": "B", "backend": "wine"}}
    try:
        page.refresh(profiles)
        first = page._cards["a"]
        changed = {k: dict(v) for k, v in profiles.items()}
        change(changed)
        page.refresh(changed)
        assert page._cards.get("a") is not first
    finally:
        page.deleteLater()


def test_new_or_replaced_art_rebuilds(qapp, tmp_path):
    page = _page(qapp)
    art = tmp_path / "bg.png"
    art.write_bytes(b"x")
    profiles = {"a": {"title": "A", "backend": "wine", "custom_background": str(art)}}
    try:
        page.refresh(profiles)
        first = page._cards["a"]
        page.refresh(profiles)
        assert page._cards["a"] is first
        stamp = os.stat(art).st_mtime_ns + 5_000_000
        os.utime(art, ns=(stamp, stamp))  # the same file name, new picture
        page.refresh(profiles)
        assert page._cards["a"] is not first
    finally:
        page.deleteLater()
