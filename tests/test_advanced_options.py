# SPDX-License-Identifier: GPL-3.0-or-later
"""Renderer, Display backend and Launch arguments are for people who know (b21 polish)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import get, kinds
from nadare.core.config import Config
from nadare.gui.settings import PLAYER_ADVANCED, SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _load(tmp_path, kind):
    config = Config.load(tmp_path / f"{kind}.json")
    config.add_profile("g", backend=kind, title="G")
    page = SettingsPage()
    page.load(config, "g")
    return page


def _shown(page, widget) -> bool:
    """Whether ``widget`` is visible within its own settings tab (tabs not on screen count)."""
    for index in range(page.tabs.count()):
        tab = page.tabs.widget(index)
        if tab.isAncestorOf(widget):
            return widget.isVisibleTo(tab)
    raise AssertionError("widget is not in any tab")


def test_the_three_options_are_the_advanced_ones():
    assert PLAYER_ADVANCED == {"renderer", "display", "launch_args"}


def test_they_are_hidden_until_show_advanced_is_ticked(tmp_path, qapp):
    checked = 0
    for kind in kinds():
        page = _load(tmp_path, kind)
        try:
            for key in PLAYER_ADVANCED:
                widget = page.option_widget(key)
                if widget is None:
                    continue
                checked += 1
                assert not _shown(page, widget), f"{kind}:{key} should start hidden"
                page.show_advanced.setChecked(True)
                assert _shown(page, widget), f"{kind}:{key} should show when advanced is on"
                page.show_advanced.setChecked(False)
        finally:
            page.deleteLater()
            qapp.processEvents()
    assert checked, "no backend has these options any more: update this test"


def test_everyday_options_stay_visible(tmp_path, qapp):
    page = _load(tmp_path, "wine")
    try:
        visible = [
            option.key
            for option in get("wine").options
            if option.key not in PLAYER_ADVANCED
            and not option.advanced
            and page.option_widget(option.key) is not None
            and _shown(page, page.option_widget(option.key))
        ]
        assert visible, "the plain options must not all be hidden"
    finally:
        page.deleteLater()
