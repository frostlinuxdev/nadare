# SPDX-License-Identifier: GPL-3.0-or-later
"""The Downloads page: every running, waiting and paused install in one list (#773)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLabel
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


def pills(row) -> list[QLabel]:
    """The state badges a row actually shows (exactly one, #b29)."""
    return [w for w in row.findChildren(QLabel) if w.objectName() == "Pill" and w.isVisibleTo(row)]


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
            running("b", operation="repair"),
            waiting("c", operation="install"),
            paused("d", operation="install"),
        ]
    )
    # One state badge per row: what runs names its operation, a waiting or paused row
    # says that instead of claiming an install is going on (#b29).
    assert page.row("a").badge.text() == "UPDATING"
    assert page.row("b").badge.text() == "REPAIRING"
    assert page.row("c").badge.text() == "WAITING"
    assert page.row("d").badge.text() == "PAUSED"


@pytest.mark.parametrize(
    ("operation", "word"),
    [("install", "Install"), ("update", "Update"), ("repair", "Repair")],
)
def test_a_waiting_row_names_its_operation_in_the_row_text(page, operation, word):
    page.set_rows([running("a"), waiting("b", ahead=2, operation=operation)])
    row = page.row("b")
    # The badge still says only the state; the operation is named in the line below.
    assert row.badge.text() == "WAITING"
    assert row.status_label.text() == f"{word} · 2 installs ahead"


@pytest.mark.parametrize(
    ("operation", "word"),
    [("install", "Install"), ("update", "Update"), ("repair", "Repair")],
)
def test_a_paused_row_names_its_operation_in_the_row_text(page, operation, word):
    page.set_rows([paused("c", operation=operation)])
    row = page.row("c")
    assert row.badge.text() == "PAUSED"
    assert row.status_label.text().startswith(f"{word} · Paused")


def test_the_queue_line_counts_one_and_many(page):
    page.set_rows([running("a"), waiting("b", ahead=1), waiting("c", ahead=2)])
    assert page.row("b").status_label.text() == "Install · 1 install ahead"
    assert page.row("c").status_label.text() == "Install · 2 installs ahead"


def test_the_queue_line_falls_back_to_the_english_other_form_without_a_catalogue(
    page, monkeypatch, tmp_path
):
    """No catalogue: the source key itself must be the English ``other`` form (#b29)."""
    from nadare.core import i18n

    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    monkeypatch.setattr(i18n, "_cache", {})
    page.set_rows([running("a"), waiting("c", ahead=2)])
    assert page.row("c").status_label.text() == "Install · 2 installs ahead"


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
    # Not cut mid-letter either: the line ends in an ellipsis (#b29).
    assert page.row("a").status_label.display_text().endswith("…")
    assert page.row("a").status_label.text() == long_name


def test_cancel_says_what_is_downloaded_stays_on_disk_where_that_is_true(page):
    # A download that can resume keeps its files when cancelled; the tooltip must say so, and a
    # row that cannot resume keeps the plain wording.
    page.set_rows([running("a"), waiting("b", can_now=False, can_pause=False), paused("c")])
    tips = {name: page.row(name).cancel_btn.toolTip() for name in ("a", "b", "c")}
    assert "stays on disk" in tips["a"] and "Resume" in tips["a"]
    assert "stays on disk" in tips["c"]
    assert "stays on disk" not in tips["b"]


# ---- b29: one state badge per row, and rows that fit at the normal width -------------


def test_a_waiting_row_says_waiting_instead_of_installing(page):
    page.set_rows([running("a"), waiting("b", ahead=1)])
    row = page.row("b")
    assert [label.text() for label in pills(row)] == ["WAITING"]
    # The badge carries the state, so the line under it does not repeat "Waiting".
    assert "waiting" not in row.status_label.text().lower()
    # The jump-the-queue offer is the waiting row's, and it must read as a waiting row.
    assert shown(row.now_btn)


@pytest.mark.parametrize(
    ("row", "text", "state"),
    [
        (running("a"), "INSTALLING", "info"),
        (waiting("b", ahead=0), "WAITING", "muted"),
        (waiting("c", ahead=2), "WAITING", "muted"),
        (paused("d"), "PAUSED", "muted"),
        (DownloadRow(profile="e", title="E", operation="install", state="finished"),
         "FINISHED", "good"),
        (DownloadRow(profile="f", title="F", operation="install", state="elsewhere"),
         "BUSY ELSEWHERE", "muted"),
        (DownloadRow(profile="g", title="G", operation="runner", state="running"),
         "PROTON RUNNER", "info"),
    ],
    ids=["running", "waiting-next", "waiting-behind", "paused", "finished", "elsewhere", "runner"],
)
def test_every_row_shows_exactly_one_state_badge(page, row, text, state):
    page.set_rows([row])
    labels = pills(page.row(row.profile))
    assert len(labels) == 1
    assert labels[0].text() == text
    assert labels[0].property("state") == state


def test_one_row_widget_carries_every_state_without_a_stale_badge(page, qapp):
    """The same DownloadRowWidget goes waiting -> running -> waiting -> paused -> running."""
    page.set_rows([running("a"), waiting("b", ahead=1, title="Strinova")])
    qapp.processEvents()
    row = page.row("b")

    def expecting(text: str, state: str, now: bool) -> None:
        assert page.row("b") is row  # reused, not rebuilt
        labels = pills(row)
        assert len(labels) == 1
        assert labels[0].text() == text
        assert labels[0].property("state") == state
        assert shown(row.now_btn) is now
        if text == "WAITING":
            visible = [w.text() for w in row.findChildren(QLabel) if w.isVisibleTo(row)]
            assert not any("Installing" in one for one in visible)

    expecting("WAITING", "muted", True)
    assert not shown(row.bar)

    page.set_rows([running("a"), running("b", title="Strinova")])  # swap: b runs
    expecting("INSTALLING", "info", False)
    assert shown(row.bar)

    page.set_rows([running("a"), waiting("b", ahead=0, title="Strinova")])  # swap back
    expecting("WAITING", "muted", True)
    assert not shown(row.bar)

    page.set_rows([paused("b", title="Strinova")])
    expecting("PAUSED", "muted", False)
    assert shown(row.resume_btn) and not shown(row.bar)

    page.set_rows([running("b", title="Strinova")])
    expecting("INSTALLING", "info", False)
    assert shown(row.bar)

    page.set_rows([])  # cancelled: the row leaves the list with its badge
    assert page.row("b") is None


def test_download_now_is_only_offered_by_a_waiting_row(page):
    page.set_rows(
        [
            running("a", can_now=True),
            waiting("b", ahead=0, can_now=True),
            paused("c", can_now=True),
            DownloadRow(
                profile="d", title="D", operation="install", state="finished", can_now=True
            ),
        ]
    )
    assert not shown(page.row("a").now_btn)  # nothing to jump while it installs
    assert shown(page.row("b").now_btn)
    assert not shown(page.row("c").now_btn)
    assert not shown(page.row("d").now_btn)


LONG_TITLE = (
    "Blue Protocol: Star Resonance - Global Release, Deluxe Edition, with the "
    "pre-registration bonus pack, the founder's weapon set and the launch soundtrack "
    "collection"
)


@pytest.mark.parametrize("width", [1280, 1024])
def test_the_title_is_never_cut_off_without_an_ellipsis(page, qapp, width):
    page.resize(width, 700)
    page.set_rows([running("a", title=LONG_TITLE)])
    qapp.processEvents()
    label = page.row("a").title_label
    assert label.toolTip() == LONG_TITLE
    assert label.display_text().endswith("…")
    assert label.display_text() != LONG_TITLE
    assert (
        label.fontMetrics().horizontalAdvance(label.display_text())
        <= label.contentsRect().width()
    )


@pytest.mark.parametrize("width", [1280, 1024])
def test_the_queue_line_is_shown_in_full(page, qapp, width):
    page.resize(width, 700)
    page.set_rows([running("a"), waiting("b", ahead=0), waiting("c", ahead=2)])
    qapp.processEvents()
    status = page.row("c").status_label
    assert "2 installs ahead" in status.text()
    assert status.display_text() == status.text()  # nothing was elided
    assert (
        status.fontMetrics().horizontalAdvance(status.display_text())
        <= status.contentsRect().width()
    )


def test_a_title_that_fits_again_is_shown_in_full_after_the_window_widens(page, qapp):
    title = "Blue Protocol: Star Resonance Deluxe Edition with the bonus pack"
    page.resize(700, 700)
    page.set_rows([running("a", title=title)])
    qapp.processEvents()
    label = page.row("a").title_label
    assert label.display_text().endswith("…")
    assert label.toolTip() == title
    page.resize(1280, 700)
    qapp.processEvents()
    assert label.display_text() == title
    # It fits now: no leftover tooltip from the narrow state.
    assert label.toolTip() == ""


def test_the_badge_sits_beside_a_short_title(page, qapp):
    page.set_rows([waiting("b", title="Strinova")])
    qapp.processEvents()
    row = page.row("b")
    title, badge = row.title_label, row.badge
    assert title.display_text() == "Strinova"
    # The title only takes the room its text needs, so the badge follows it instead of
    # being pushed to the far edge of the row (#b29).
    assert title.width() <= title.sizeHint().width()
    assert badge.x() - (title.x() + title.width()) == 10
    assert badge.x() + badge.width() < row.width() // 2


def test_a_long_title_still_elides_beside_its_badge(page, qapp):
    page.set_rows([waiting("c", title=LONG_TITLE, ahead=1)])
    qapp.processEvents()
    row = page.row("c")
    label = row.title_label
    assert label.display_text().endswith("…")
    assert label.display_text() != LONG_TITLE
    assert label.toolTip() == LONG_TITLE
    assert (
        label.fontMetrics().horizontalAdvance(label.display_text())
        <= label.contentsRect().width()
    )
    assert row.badge.x() >= label.x() + label.width()


def test_a_title_that_fits_has_no_tooltip_after_apply_and_update_progress(page, qapp):
    page.set_rows([running("a", title="Strinova")])
    qapp.processEvents()
    row = page.row("a")
    row.apply(running("a", title="Strinova"))
    # OneLineLabel owns the tooltip: it only appears when the text is elided.
    assert row.title_label.toolTip() == ""
    page.update_progress("a", 0.5, "Downloading 1 GiB", "about 2 min left")
    qapp.processEvents()
    assert row.title_label.toolTip() == ""
    assert row.status_label.toolTip() == ""


def test_a_one_line_label_re_elides_when_its_font_grows_without_a_resize(qapp):
    from nadare.gui.game_view_parts.shared import OneLineLabel

    label = OneLineLabel("Blue Protocol: Star Resonance")
    label.setStyleSheet("font-size: 9px;")
    label.setFixedWidth(200)
    label.show()
    qapp.processEvents()
    assert label.display_text() == label.text()
    width = label.width()

    label.setStyleSheet("font-size: 24px;")  # no resize follows
    qapp.processEvents()
    assert label.width() == width
    assert label.display_text().endswith("…")
    assert label.display_text() != label.text()
    assert label.toolTip() == label.text()
    label.deleteLater()


def test_the_progress_bar_does_not_jiggle_with_the_transfer_text(page, qapp):
    page.set_rows([running("a", eta="1 MiB/s")])
    qapp.processEvents()
    row = page.row("a")
    before = row.bar.geometry()
    page.update_progress(
        "a", 0.5, "Downloading 1 GiB", "53.0 MiB/s · 32 connections · about 10 min left"
    )
    qapp.processEvents()
    assert row.bar.geometry() == before
    page.update_progress("a", 0.6, "Downloading 1 GiB", "1.2 GiB/s")
    qapp.processEvents()
    assert row.bar.geometry() == before

