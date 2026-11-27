# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings search follow-up (#1810): the query echo and stale results.

The review of 2026-11-26 found that the "Nothing matches" line rendered the raw
query as rich text, stretched the page with a long query, and kept stale results
when a searched row's text changed without a show/hide (the Disk space
measurement landing during a search).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import storagecard
from nadare.core.config import Config
from nadare.gui.disk_space_panel import DiskSpacePanel
from nadare.gui.settings import SettingsPage
from nadare.gui.settings_parts import search as search_parts


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    # Never walk the host folders: this test only cares about the text a
    # measurement would leave behind.
    monkeypatch.setattr(DiskSpacePanel, "refresh", lambda self: None)
    # Nor start the page's background probes (SELinux module list, prefix lock): their
    # late results would re-filter the page and could hide a missing texts_changed.
    monkeypatch.setattr(SettingsPage, "_check_fgo_selinux_module", lambda self: None)
    monkeypatch.setattr(SettingsPage, "_refresh_prefix_lock", lambda self, *a, **k: None)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    widget = SettingsPage()
    widget.load(config, "sample")
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _shown(page: SettingsPage, widget) -> bool:
    for index in range(page.tabs.count()):
        tab = page.tabs.widget(index)
        if tab.isAncestorOf(widget):
            return widget.isVisibleTo(tab)
    return widget.isVisibleTo(page)


# ---- S1/S2: the query is shown literally and elided ------------------------------------
def test_the_no_hits_line_shows_the_query_as_plain_text(page):
    page.search.setText("<b>x</b>")
    assert page.search_empty.textFormat() == Qt.TextFormat.PlainText
    assert page.search_empty.text() == "Nothing matches “<b>x</b>”"


def test_an_image_tag_in_the_query_is_not_loaded(page, tmp_path, monkeypatch):
    # A short relative path, so the whole tag survives the echo's cut and a rich-text
    # label really would decode the picture (Neon r1: a long tmp path was elided first).
    monkeypatch.chdir(tmp_path)
    picture = QImage(400, 300, QImage.Format.Format_RGB32)
    picture.fill(0xFF102030)
    assert picture.save("h.png")
    page.search.setText('<img src="h.png">')
    assert '<img src="h.png">' in page.search_empty.text()
    # Plain text never decodes the file: the label stays a single text line.
    assert page.search_empty.textFormat() == Qt.TextFormat.PlainText
    assert page.search_empty.heightForWidth(1000) < 60


def test_a_long_query_is_elided_and_does_not_stretch_the_line(page):
    query = "x" * 5000
    page.search.setText(query)
    assert page.search_empty.isVisibleTo(page)
    text = page.search_empty.text()
    assert text.startswith("Nothing matches “")
    assert text.endswith("”")
    assert "…" in text
    assert query not in text
    assert len(text) < 250
    # A 32 k paste used to make the label ask for >200 000 px of width.
    assert page.search_empty.minimumSizeHint().width() < 900


def test_the_echoed_query_keeps_its_start_and_is_not_huge(page):
    query = "find-me " + "y" * 4000
    page.search.setText(query)
    text = page.search_empty.text()
    assert "find-me" in text
    assert len(text) < 250


def test_the_echo_never_measures_the_whole_paste(page, monkeypatch):
    seen: list[str] = []

    class Recorder:
        def __init__(self, _font) -> None:
            pass

        def elidedText(self, text, _mode, _width):  # noqa: N802 - Qt naming
            seen.append(text)
            return text

    monkeypatch.setattr(search_parts, "QFontMetrics", Recorder)
    page.search.setText("x" * 32700)
    assert seen
    assert max(len(text) for text in seen) <= search_parts._QUERY_ECHO_CHARS + 1


# ---- S3: a text-only change refreshes the active filter --------------------------------
def test_a_measurement_landing_refreshes_the_results(page, qapp):
    panel = page.disk_space_panel
    page.search.setText("could not check")
    assert not _shown(page, panel)
    assert page.search_empty.isVisibleTo(page)

    # The measurement lands and only updates text; it hides/shows nothing.
    panel._on_measured(panel._generation, [], [], None)
    qapp.processEvents()

    assert _shown(page, panel)
    assert not page.search_empty.isVisibleTo(page)


def test_a_finished_measurement_refreshes_the_results(page, qapp, tmp_path):
    panel = page.disk_space_panel
    page.search.setText("installers · 7 B")
    assert not _shown(page, panel)
    assert page.search_empty.isVisibleTo(page)

    report = storagecard.Report(
        installers=storagecard.Installers((tmp_path / "x.exe",), 7)
    )
    panel._on_measured(panel._generation, [], [], report)
    qapp.processEvents()

    assert _shown(page, panel)
    assert not page.search_empty.isVisibleTo(page)


# ---- Neon r1 ----------------------------------------------------------------------------
def test_the_search_box_takes_a_bounded_query(page):
    page.search.setText("e " * 16383)
    assert len(page.search.text()) <= 256


def test_repeated_words_are_matched_once():
    from nadare.gui.settings_parts import search

    assert search._query_words("Wine wine  WINE prefix") == ["wine", "prefix"]


def test_the_echoed_query_stays_on_one_line(page):
    page.search.setText("x\n" * 50 + "zzzq")
    shown = page.search_empty.text()
    assert page.search_empty.isVisibleTo(page)
    assert not any(c in shown for c in "\n\r\u2028\u2029")


def test_a_recovered_settings_file_still_refilters_an_active_search(qapp, tmp_path):
    panel = DiskSpacePanel()
    try:
        config = Config.load(tmp_path / "config.json")
        config.notice = "recovered"
        fired = []
        panel.texts_changed.connect(lambda: fired.append(True))
        panel.load(config)
        panel.refresh()
        assert fired
    finally:
        panel.deleteLater()
