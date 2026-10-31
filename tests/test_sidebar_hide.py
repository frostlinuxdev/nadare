# SPDX-License-Identifier: GPL-3.0-or-later
"""#532: an optional sidebar setting that hides games which are not installed."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core.backends import Status  # noqa: E402
from nadare.core.config import TOP_DEFAULTS, Config  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402
from nadare.gui.settings import SettingsPage  # noqa: E402
from nadare.gui.sidebar import SidebarGames  # noqa: E402


def _profiles(*names):
    return {n: {"backend": "wine", "title": n.title()} for n in names}


def _sidebar(qapp, hide, **statuses):
    side = SidebarGames()
    side.set_hide_uninstalled(hide)
    side.set_profiles(_profiles("alpha", "beta", "gamma", "delta"))
    for name, state in statuses.items():
        side.set_status(name, Status(state))
    return side


def test_the_default_shows_everything(qapp):
    assert TOP_DEFAULTS["sidebar_hide_uninstalled"] is False
    side = _sidebar(qapp, False, alpha="installed", beta="not_installed")
    assert set(side.profiles()) == {"alpha", "beta", "gamma", "delta"}


def test_hiding_removes_only_known_uninstalled_games(qapp):
    side = _sidebar(qapp, True, alpha="installed", beta="not_installed", gamma="error")
    # delta has no status yet: shown, so rows do not pop in and out while statuses arrive
    assert set(side.profiles()) == {"alpha", "delta", "gamma"}


def test_running_and_updatable_games_are_kept(qapp):
    side = SidebarGames()
    side.set_hide_uninstalled(True)
    side.set_profiles(_profiles("run", "upd", "gone"))
    side.set_status("run", Status("running", running=True))
    side.set_status("upd", Status("not_installed", data={"update_available": True}))
    side.set_status("gone", Status("not_installed"))
    assert set(side.profiles()) == {"run", "upd"}


def test_the_open_game_stays_listed_even_if_not_installed(qapp):
    side = _sidebar(qapp, True, beta="not_installed")
    side.set_active("beta")
    side.set_status("beta", Status("not_installed"))
    assert "beta" in side.profiles()
    side.set_active("")
    side.set_status("beta", Status("not_installed"))
    assert "beta" not in side.profiles()


def test_leaving_a_not_installed_game_hides_it_again_without_another_status(qapp):
    side = _sidebar(qapp, True, beta="not_installed")
    side.set_active("beta")
    side.set_status("beta", Status("not_installed"))
    assert "beta" in side.profiles()
    side.set_active("")  # back to the library: nothing else changes
    assert "beta" not in side.profiles()


def test_opening_a_hidden_game_adds_its_row_at_once(qapp):
    side = _sidebar(qapp, True, alpha="installed", beta="not_installed")
    assert "beta" not in side.profiles()
    side.set_active("beta")  # no status arrives afterwards
    assert "beta" in side.profiles()


def test_turning_the_setting_off_brings_the_games_back(qapp):
    side = _sidebar(qapp, True, alpha="installed", beta="not_installed")
    assert "beta" not in side.profiles()
    side.set_hide_uninstalled(False)
    assert "beta" in side.profiles()


def test_the_header_hides_when_nothing_is_left(qapp):
    side = SidebarGames()
    side.set_hide_uninstalled(True)
    side.set_profiles(_profiles("only"))
    side.set_status("only", Status("not_installed"))
    assert side.profiles() == [] and side.header.isHidden()


# ----------------------------------------------------------------- settings page
def _page(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    config.save()
    page = SettingsPage()
    page.load(config, "p")
    return page, config


def test_the_settings_checkbox_defaults_off_and_round_trips(qapp, tmp_path):
    page, config = _page(tmp_path)
    assert page.hide_uninstalled.isChecked() is False
    page.hide_uninstalled.setChecked(True)
    page.save()
    assert config.data["sidebar_hide_uninstalled"] is True
    assert Config.load(tmp_path / "config.json").data["sidebar_hide_uninstalled"] is True
    page.load(config, "p")
    assert page.hide_uninstalled.isChecked() is True


def test_changing_only_the_checkbox_counts_as_an_unsaved_change(qapp, tmp_path):
    page, _config = _page(tmp_path)
    before = page._form_state()
    page.hide_uninstalled.setChecked(True)
    assert page._form_state() != before


# ------------------------------------------------------------------ main window
def _window(tmp_path, monkeypatch, **values):
    from nadare.gui import workers

    monkeypatch.setattr(workers.BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="wine", title="A")
    config.add_profile("b", backend="wine", title="B")
    config.data.update(values)
    return MainWindow(config)


def test_the_window_applies_the_saved_setting_at_start(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch, sidebar_hide_uninstalled=True)
    try:
        window._on_result("a", Status("installed"))
        window._on_result("b", Status("not_installed"))
        assert window.sidebar_games.profiles() == ["a"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_saving_the_setting_updates_the_sidebar_at_once(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window._on_result("a", Status("installed"))
        window._on_result("b", Status("not_installed"))
        assert set(window.sidebar_games.profiles()) == {"a", "b"}
        window.config.data["sidebar_hide_uninstalled"] = True
        window._on_settings_saved("a")
        assert window.sidebar_games.profiles() == ["a"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
