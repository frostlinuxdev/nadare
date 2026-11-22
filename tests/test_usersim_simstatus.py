# SPDX-License-Identifier: GPL-3.0-or-later
"""The game header follows live operations and reuses Store art without a fetch."""

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QMessageBox

from nadare.core import artwork
from nadare.core.backends import Status
from nadare.core.events import Event
from nadare.gui.game_view import GameView
from nadare.gui.main_window_parts.window_state import WindowStateMixin


@pytest.fixture
def view(_one_qapplication):
    page = GameView()
    page.set_profile("sample", {"title": "Sample", "backend": "direct-arknights"})
    yield page
    page._timer.stop()
    page.deleteLater()


@pytest.mark.parametrize("operation", ["install", "update", "repair"])
@pytest.mark.parametrize(
    "idle", ["Sample is not installed yet", "Installation incomplete; click RESUME"]
)
def test_live_header_replaces_idle_and_tracks_steps(view, operation, idle):
    view.set_status(Status("not_installed", message=idle))
    view.set_busy(operation)
    assert idle not in view.detail_label.text()
    view.append_event(Event("status", "unpacking runner"))
    view.append_event(Event("step", "Downloading game files"))
    assert view.detail_label.text() == "Downloading game files"
    # The byte counter belongs to the progress bar below; the header keeps the phase.
    view.append_event(Event("progress", "download: 1.6 GiB / 3.5 GiB", progress=0.4))
    assert view.detail_label.text() == "Downloading game files"
    # A delayed status probe, found-copy hint or leftover hint cannot replace live progress.
    view.set_status(Status("not_installed", message=idle, data={"leftover_files": 3}))
    view.set_found_copy("/a/copy")
    assert view.detail_label.text() == "Downloading game files"
    view.append_event(Event("done", "Download finished"))
    assert view.detail_label.text() == ""


@pytest.mark.parametrize("ending", ["finish", "fail", "cancelled", "set_paused"])
def test_idle_header_returns_on_every_end(view, ending):
    idle = "Installation incomplete; click RESUME"
    view.set_status(Status("not_installed", message=idle))
    view.set_busy("install")
    view.append_event(Event("status", "unpacking runner"))
    if ending == "finish":
        view.finish("install", 0)
    elif ending == "fail":
        view.fail("Failed", quiet=True)
    elif ending == "set_paused":
        view.set_paused("install")
    else:
        view.cancelled()
    assert view.detail_label.text() == idle
    view.set_status(Status("installed", message="Ready to play"))
    assert view.detail_label.text() == "Ready to play"


def test_header_has_readable_backing_over_white_art(view):
    image = QPixmap(1600, 640)
    image.fill(QColor("white"))
    view.hero.set_source(image)
    view.set_status(Status("not_installed", message="Installation incomplete; click RESUME"))
    effect = view.detail_label.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect)
    assert effect.color().alpha() >= 200
    # A dark backing gives small status text reliable contrast even on white key art.
    style = view.detail_label.styleSheet()
    assert "background-color: rgba(" in style and "border-radius" in style
    assert "color: #ffffff" in style or "color: rgba(255, 255, 255" in style


def test_header_backing_hugs_its_text_not_the_page(view):
    """The backing is a small pill, not a page-wide black strip that reads as a second bar."""
    view.resize(1800, 900)
    view.show()
    view.set_busy("install")
    view.append_event(Event("step", "Downloading game files"))
    view.append_event(Event("progress", "install: 366 MiB / 33 GiB", progress=0.01))
    from PyQt6.QtWidgets import QApplication

    QApplication.processEvents()
    label = view.detail_label
    assert label.isVisible()
    assert label.width() < label.fontMetrics().horizontalAdvance(label.text()) + 80
    assert label.width() < view.progress.width() / 2
    view.hide()


@pytest.mark.parametrize("kind", ["cover-wide", "tile"])
def test_install_reuses_the_store_cached_art(view, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: pytest.fail("GUI fetched art"))
    image = QPixmap(400, 300)
    image.fill(QColor("#fb4500"))
    path = tmp_path / f"aether-gazer-{kind}.png"
    assert image.save(str(path))
    view.set_profile("ag", {"title": "Aether Gazer", "backend": "direct-aether-gazer"})
    view.set_busy("install")
    view.wait_for_images()
    pixel = view.art_label.pixmap().toImage().pixelColor(100, 128)
    assert pixel.red() > 240 and pixel.green() < 90 and pixel.blue() < 20


def test_close_warning_names_resume_and_startup_continue(monkeypatch):
    seen = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a: seen.append(a[2]) or QMessageBox.StandardButton.Cancel
    )
    window = SimpleNamespace(
        controller=SimpleNamespace(
            launching_profiles=lambda: [],
            mutating_profiles=lambda: ["sample"],
            running_operation=lambda name: "install",
            queue_order=lambda: [],
        ),
        _busy={},
        _game_title=lambda name: "Sample",
    )
    assert WindowStateMixin._confirm_close(window) is False
    assert "RESUME carries on" in seen[0]
    assert "continue downloads" in seen[0].lower()
    assert "INSTALL carries on" not in seen[0]
