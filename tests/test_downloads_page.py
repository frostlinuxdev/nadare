# SPDX-License-Identifier: GPL-3.0-or-later
"""The Downloads page: every running, waiting and paused install in one list (#773)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.downloads_page import DownloadRow, DownloadsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qapp):
    widget = DownloadsPage()
    widget.resize(900, 600)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def shown(widget) -> bool:
    return widget.isVisibleTo(widget.window())


def running(profile="a", **kw):
    base = dict(
        profile=profile, title=profile.upper(), operation="install", state="running",
        detail="Downloading", fraction=0.4, eta="12 min left", can_pause=True,
    )
    base.update(kw)
    return DownloadRow(**base)


def waiting(profile="b", ahead=0, **kw):
    base = dict(
        profile=profile, title=profile.upper(), operation="install", state="waiting",
        ahead=ahead, can_now=True,
    )
    base.update(kw)
    return DownloadRow(**base)


def paused(profile="c", **kw):
    base = dict(profile=profile, title=profile.upper(), operation="install", state="paused")
    base.update(kw)
    return DownloadRow(**base)


def test_an_empty_page_says_nothing_is_downloading(page):
    page.set_rows([])
    assert shown(page.empty_label)
    assert "No downloads" in page.empty_label.text()
    assert page.row_profiles() == []


def test_rows_replace_the_empty_message_and_keep_their_order(page):
    page.set_rows([running("a"), waiting("b"), paused("c")])
    assert not shown(page.empty_label)
    assert page.row_profiles() == ["a", "b", "c"]


def test_a_running_row_shows_progress_pause_and_cancel(page):
    page.set_rows([running("a")])
    row = page.row("a")
    assert row.title_label.text() == "A"
    assert row.bar.value() == 40 and shown(row.bar)
    assert "12 min left" in row.eta_label.text()
    assert shown(row.pause_btn) and shown(row.cancel_btn)
    assert not any(shown(b) for b in (row.resume_btn, row.next_btn, row.now_btn))


def test_a_running_row_that_cannot_pause_hides_pause(page):
    page.set_rows([running("a", can_pause=False)])
    assert not shown(page.row("a").pause_btn)
    assert shown(page.row("a").cancel_btn)


def test_a_running_row_without_a_fraction_shows_a_busy_bar(page):
    page.set_rows([running("a", fraction=None)])
    assert page.row("a").bar.maximum() == 0


def test_the_first_waiting_row_offers_download_now_but_not_next(page):
    page.set_rows([running("a"), waiting("b", ahead=0)])
    row = page.row("b")
    assert "Next in line" in row.status_label.text()
    assert shown(row.now_btn) and not shown(row.next_btn)
    assert not shown(row.bar)


def test_a_later_waiting_row_offers_next_and_says_how_many_are_ahead(page):
    page.set_rows([running("a"), waiting("b", ahead=0), waiting("c", ahead=1)])
    row = page.row("c")
    assert "1 install ahead" in row.status_label.text()
    assert shown(row.next_btn)
    page.set_rows([running("a"), waiting("b", ahead=0), waiting("c", ahead=2)])
    assert "2 installs ahead" in page.row("c").status_label.text()


def test_download_now_is_hidden_when_nothing_running_can_pause(page):
    page.set_rows([running("a", can_pause=False), waiting("b", can_now=False)])
    assert not shown(page.row("b").now_btn)


def test_a_paused_row_offers_resume_and_cancel_only(page):
    page.set_rows([paused("c")])
    row = page.row("c")
    assert "Paused" in row.status_label.text()
    assert shown(row.resume_btn) and shown(row.cancel_btn)
    assert not any(shown(b) for b in (row.pause_btn, row.next_btn, row.now_btn, row.bar))


def test_the_operation_is_named_in_plain_words(page):
    page.set_rows(
        [
            running("a", operation="update"),
            waiting("b", operation="repair"),
            paused("c", operation="install"),
        ]
    )
    assert "Updating" in page.row("a").kind_label.text()
    assert "Repairing" in page.row("b").kind_label.text()
    assert "Installing" in page.row("c").kind_label.text()


@pytest.mark.parametrize(
    ("rows", "profile", "button", "signal"),
    [
        ([running("a")], "a", "pause_btn", "pause_requested"),
        ([running("a")], "a", "cancel_btn", "cancel_requested"),
        (
            [running("a"), waiting("b", ahead=0), waiting("c", ahead=1)],
            "c",
            "next_btn",
            "next_requested",
        ),
        ([running("a"), waiting("b")], "b", "now_btn", "now_requested"),
        ([paused("c")], "c", "resume_btn", "resume_requested"),
    ],
)
def test_each_button_asks_for_its_action_on_its_own_game(page, rows, profile, button, signal):
    page.set_rows(rows)
    got: list[str] = []
    getattr(page, signal).connect(got.append)
    getattr(page.row(profile), button).click()
    assert got == [profile]


def test_clicking_a_row_opens_that_game(page, qapp):
    page.set_rows([running("a"), paused("c")])
    got: list[str] = []
    page.open_requested.connect(got.append)
    QTest.mouseClick(page.row("c"), Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
    assert got == ["c"]


def test_the_same_rows_are_updated_in_place(page):
    page.set_rows([running("a", fraction=0.1), waiting("b")])
    first = page.row("a")
    page.set_rows([running("a", fraction=0.7), waiting("b")])
    assert page.row("a") is first
    assert first.bar.value() == 70


def test_a_row_that_changes_state_gets_the_right_buttons(page):
    page.set_rows([running("a")])
    assert shown(page.row("a").pause_btn)
    page.set_rows([paused("a")])
    assert not shown(page.row("a").pause_btn) and shown(page.row("a").resume_btn)


def test_rows_that_are_gone_are_removed(page):
    page.set_rows([running("a"), waiting("b")])
    page.set_rows([waiting("b")])
    assert page.row_profiles() == ["b"]
    assert page.row("a") is None


def test_progress_updates_one_row_without_rebuilding(page):
    page.set_rows([running("a", fraction=0.1), running("b", fraction=0.2)])
    row = page.row("a")
    page.update_progress("a", 0.55, "Downloading 5 GiB", "3 min left")
    assert page.row("a") is row
    assert row.bar.value() == 55 and "3 min left" in row.eta_label.text()
    assert row.status_label.text() == "Downloading 5 GiB"
    assert page.row("b").bar.value() == 20


def test_progress_for_a_game_not_listed_is_ignored(page):
    page.set_rows([running("a")])
    page.update_progress("zzz", 0.5, "x", "y")  # no error, nothing changes
    assert page.row_profiles() == ["a"]


def test_titles_with_markup_are_shown_as_text(page):
    page.set_rows([running("a", title="<b>Evil</b>")])
    assert page.row("a").title_label.text() == "<b>Evil</b>"
    assert page.row("a").title_label.textFormat() == Qt.TextFormat.PlainText


def test_a_waiting_row_can_be_paused_when_its_download_resumes(page):
    page.set_rows([running("a"), waiting("b", can_pause=True), waiting("c", can_pause=False)])
    assert shown(page.row("b").pause_btn)
    assert not shown(page.row("c").pause_btn)


def test_a_long_step_text_does_not_make_the_row_taller(page, qapp):
    page.set_rows([running("a", detail="installing"), waiting("b")])
    qapp.processEvents()
    short = page.row("a").height()
    long_name = "installing " + "AetherGazer_jp_us_prod_293_windows_limit_sdk_release_" * 4
    page.set_rows([running("a", detail=long_name), waiting("b")])
    qapp.processEvents()
    assert page.row("a").height() == short
    assert page.row("a").status_label.toolTip() == long_name  # the full text stays reachable


def test_cancel_says_what_is_downloaded_stays_on_disk_where_that_is_true(page):
    # A download that can resume keeps its files when cancelled; the tooltip must say so, and a
    # row that cannot resume keeps the plain wording.
    page.set_rows([running("a"), waiting("b", can_now=False, can_pause=False), paused("c")])
    tips = {name: page.row(name).cancel_btn.toolTip() for name in ("a", "b", "c")}
    assert "stays on disk" in tips["a"] and "Resume" in tips["a"]
    assert "stays on disk" in tips["c"]
    assert "stays on disk" not in tips["b"]
