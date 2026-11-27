# SPDX-License-Identifier: GPL-3.0-or-later
"""Leftovers of the Settings search review (#1855): late text, reverse changes, a lost card."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import relink as relink_core
from nadare.core import storagecard
from nadare.core.config import Config
from nadare.gui.disk_space_panel import DiskSpacePanel
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(DiskSpacePanel, "refresh", lambda self: None)
    monkeypatch.setattr(SettingsPage, "_check_fgo_selinux_module", lambda self: None)
    monkeypatch.setattr(SettingsPage, "_refresh_prefix_lock", lambda self, *a, **k: None)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    widget = SettingsPage()
    widget.load(config, "sample")
    yield widget
    if not sip.isdeleted(widget):
        widget.deleteLater()
    qapp.processEvents()


def _shown(page: SettingsPage, widget) -> bool:
    for index in range(page.tabs.count()):
        tab = page.tabs.widget(index)
        if tab.isAncestorOf(widget):
            return widget.isVisibleTo(tab)
    return widget.isVisibleTo(page)


# ---- S3: cards whose text changes after load ------------------------------------------
def test_fetched_artwork_refilters_an_active_search(page, qapp, tmp_path):
    cover = tmp_path / "zyxwcover" / "cover.png"
    page.search.setText("zyxwcover")
    assert not _shown(page, page.background)

    page._on_art_fetched("sample", {"cover": str(cover)})
    qapp.processEvents()

    assert _shown(page, page.background)
    assert not page.search_empty.isVisibleTo(page)


def test_a_relinked_prefix_refilters_an_active_search(page, qapp, tmp_path):
    editor = page._option_widgets.get("prefix")
    assert editor is not None
    new_prefix = tmp_path / "qvzprefix"
    page.search.setText("qvzprefix")
    assert not _shown(page, editor)

    plan = relink_core.Relink(changes={"prefix": str(new_prefix)})
    assert page._commit_relink(page._config, "sample", plan)
    qapp.processEvents()

    assert _shown(page, editor)


# ---- Neon 6: reverse transitions and a card that goes away -----------------------------
def test_text_changing_back_hides_the_card_again(page, qapp, tmp_path):
    panel = page.disk_space_panel
    page.search.setText("installers · 7 B")
    report = storagecard.Report(installers=storagecard.Installers((tmp_path / "x.exe",), 7))
    panel._on_measured(panel._generation, [], [], report)
    qapp.processEvents()
    assert _shown(page, panel)

    panel._on_measured(panel._generation, [], [], None)  # the measurement is gone again
    qapp.processEvents()

    assert not _shown(page, panel)
    assert page.search_empty.isVisibleTo(page)


def test_a_card_destroyed_during_a_search_does_not_break_it(page, qapp):
    page.search.setText("folder")
    sip.delete(page.disk_space_panel)
    page.search.setText("prefix")
    qapp.processEvents()
    page.search.clear()
    qapp.processEvents()
    assert not page.search_empty.isVisibleTo(page)
