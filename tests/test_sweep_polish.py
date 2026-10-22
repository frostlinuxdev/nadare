# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the six user-facing polish fixes."""

from __future__ import annotations

import logging

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _backend_helpers import make_context

from nadare.core import artwork, graphics
from nadare.core.backends import BackendError, Status
from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui import workers
from nadare.gui.game_view import GameView
from nadare.gui.library import LibraryPage, display_title
from nadare.gui.main_window import MainWindow
from nadare.gui.settings import SettingsPage
from nadare.gui.sidebar import SidebarGames
from nadare.gui.workers import BackendWorker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 1. #286: the progress bar/step text disappear when the operation ends
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ending", ["finish-ok", "finish-error", "fail", "cancelled"])
def test_busy_indicators_are_cleared_when_the_operation_ends(qapp, monkeypatch, ending):
    from nadare.gui import game_view as game_view_mod

    monkeypatch.setattr(game_view_mod.QMessageBox, "warning", lambda *a, **k: None)
    view = GameView()
    view.set_profile("sample", {"title": "Sample"})
    try:
        view.set_busy("install")
        view.append_event(Event("progress", "Downloading", progress=0.5))
        assert not view.progress.isHidden()
        assert view.step_label.text()
        assert view.elapsed_label.text()

        if ending == "finish-ok":
            view.finish("install", 0)
        elif ending == "finish-error":
            view.finish("install", 3)
        elif ending == "fail":
            view.fail("boom")
        else:
            view.cancelled()

        assert view.progress.isHidden(), f"progress bar still visible after {ending}"
        assert view.step_label.text() == ""
        assert view.elapsed_label.text() == ""
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. #329: plain error messages and friendly operation titles
# ---------------------------------------------------------------------------
def _run_failing_worker(monkeypatch, tmp_path, exc):
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(workers, "create", boom)
    worker = BackendWorker(Config.load(tmp_path / "config.json"), "p", "install")
    failed: list[str] = []
    worker.failed.connect(failed.append)
    return worker, failed


def test_worker_known_error_is_not_prefixed_with_the_class_name(
    monkeypatch, tmp_path, caplog
):
    worker, failed = _run_failing_worker(monkeypatch, tmp_path, BackendError("disk full"))
    with caplog.at_level(logging.WARNING, logger="nadare.gui.workers"):
        worker.run()
    assert failed == ["disk full"]
    # The class name goes to the log (the shared masker may shorten it), never
    # into the message shown to the user.
    assert caplog.records and "disk full" in caplog.text


def test_worker_unexpected_error_is_not_prefixed_with_the_class_name(
    monkeypatch, tmp_path
):
    worker, failed = _run_failing_worker(monkeypatch, tmp_path, ValueError("bad value"))
    worker.run()
    assert failed == ["bad value"]


def test_worker_keeps_the_class_name_when_the_exception_has_no_text(
    monkeypatch, tmp_path
):
    worker, failed = _run_failing_worker(monkeypatch, tmp_path, ValueError())
    worker.run()
    assert failed == ["ValueError: "]


def test_finish_uses_a_friendly_operation_title(qapp, monkeypatch):
    from nadare.gui import game_view as game_view_mod

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        game_view_mod.QMessageBox,
        "warning",
        lambda _parent, title, message: seen.append((title, message)),
    )
    view = GameView()
    try:
        view.finish("install", 3)
        assert seen == [
            (
                "Operation failed",
                "Installing did not finish (exit code 3). Open the logs for details.",
            )
        ]
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_failure_dialog_for_background_profile_uses_the_friendly_title(
    tmp_path, qapp, monkeypatch
):
    from nadare.gui import main_window as main_window_mod

    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.add_profile("beta", backend="wine", title="Beta")
    window = MainWindow(config, profile="alpha")
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window_mod.QMessageBox,
        "warning",
        lambda _parent, title, message: seen.append((title, message)),
    )
    try:
        window._on_failed("beta", "login", "boom")
        assert seen == [("Beta: Signing in", "boom")]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. #327: the language row is hidden when there is only one language
# ---------------------------------------------------------------------------
def test_language_row_is_hidden_with_one_locale(qapp, monkeypatch):
    from nadare.gui import settings as settings_mod

    monkeypatch.setattr(settings_mod.i18n, "available", lambda: ["en"])
    page = SettingsPage()
    try:
        assert page.language.isHidden()
        assert page.language_label.isHidden()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_language_combo_shows_native_names(qapp, monkeypatch):
    from nadare.gui import settings as settings_mod

    monkeypatch.setattr(settings_mod.i18n, "available", lambda: ["en", "fr"])
    page = SettingsPage()
    try:
        assert not page.language.isHidden()
        assert not page.language_label.isHidden()
        assert page.language.itemText(0) == "English"
        assert page.language.itemData(0) == "en"
        assert page.language.itemText(1) == "fr"
        assert page.language.itemData(1) == "fr"
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 4. #370: duplicate titles are disambiguated by profile name
# ---------------------------------------------------------------------------
def test_display_title_only_adds_the_profile_id_for_duplicates(qapp):
    solo = {"czn": {"title": "Chaos Zero Nightmare"}}
    assert display_title("czn", solo) == "Chaos Zero Nightmare"
    pair = {"arknights": {"title": "Arknights"}, "arknights-2": {"title": "arknights"}}
    assert display_title("arknights", pair) == "Arknights · arknights"
    assert display_title("arknights-2", pair) == "arknights · arknights-2"


def test_library_and_sidebar_cards_show_the_profile_name(qapp):
    profiles = {
        "arknights": {"title": "Arknights"},
        "arknights-2": {"title": "Arknights"},
    }
    page = LibraryPage()
    sidebar = SidebarGames()
    try:
        page.refresh(profiles)
        texts = {name: card.title_label.text() for name, card in page._cards.items()}
        assert texts["arknights"] == "Arknights · arknights"
        assert texts["arknights-2"] == "Arknights · arknights-2"

        sidebar.set_profiles(
            profiles,
            {
                "arknights": Status("installed"),
                "arknights-2": Status("installed"),
            },
        )
        assert sidebar.row("arknights").title_label.text() == "Arknights · arknights"
        assert sidebar.row("arknights-2").title_label.text() == "Arknights · arknights-2"
    finally:
        page.deleteLater()
        sidebar.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 5. #285: no Vulkan warning for a brand-new not-installed CZN profile
# ---------------------------------------------------------------------------
def test_czn_status_only_mentions_vulkan_when_installed(tmp_path, monkeypatch):
    from nadare.backends.czn.backend import CznBackend

    monkeypatch.setattr(graphics, "vulkan_note", lambda: "VULKAN NOTE")
    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    )
    monkeypatch.setattr(backend, "installed", lambda: False)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "_session_info", lambda: None)

    status = backend.status()
    assert status.state == "not_installed"
    assert "VULKAN NOTE" not in status.message

    monkeypatch.setattr(backend, "installed", lambda: True)
    status = backend.status()
    assert status.state == "installed"
    assert "VULKAN NOTE" in status.message


def test_vulkan_note_is_plain_language(monkeypatch):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    assert graphics.vulkan_note() == (
        "Your graphics driver looks old for Vulkan games. If the game will not "
        "start, open Settings and try the OpenGL renderer."
    )


# ---------------------------------------------------------------------------
# 6. #251: official-art provenance survives a restart
# ---------------------------------------------------------------------------
def test_official_hero_provenance_is_persisted(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", title="Sample")
    data = config.profile("sample")

    cache = artwork.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    cover = cache / "sample-cover.png"
    cover.write_bytes(b"cover")
    assert artwork.record_thirdparty(data, cover, "cover")
    config.save()

    official = cache / "sample-hero-official.png"
    official.write_bytes(b"hero")
    artwork.src_sidecar(official).write_text("https://example.invalid/hero.png")

    window = MainWindow(config)
    try:
        window._record_official_art("sample", str(official))
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()

    reloaded = Config.load(config.path)
    profile = reloaded.profile("sample")
    assert profile["art_source"]["hero"] == "official"
    assert artwork.resolve(profile) == official
