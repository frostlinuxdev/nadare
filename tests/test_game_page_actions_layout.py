# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page's action buttons never share a row with the switches under them.

In a real b29 run (Strinova, installing) the Sandbox checkbox was drawn on top of
"Report a problem": the hero keeps its own height (Maximum policy), but its size hint
did not follow the page width, so on a narrow page the buttons wrapped to a line the
hero had not reserved and the copy column was squeezed.

A page that is already shown and then narrowed must ask for a new height as well; a
hint cached at the old width leaves the narrow layout overlapping and leaves the wide
one bloated.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui import theme
from nadare.gui.game_view import GameView

#: Everything that may be drawn inside the hero's copy column. Hidden widgets take no place.
ACTION_WIDGETS = (
    "primary",
    "pause_button",
    "update_btn",
    "verify",
    "repair_btn",
    "safety",
    "logs",
    "report",
    "remember",
    "sandbox",
    "notice_label",
    "detail_label",
    "safety_status",
    "login",
    "logout",
)

#: The brief's two sizes down to the narrowest page the smallest window gives.
WIDTHS = (1280, 1024, 900, 800, 720, 680, 660, 640)

#: Every text size the launcher may run at, on both page shapes: BPSR has a long notice,
#: Strinova has none. The real launcher loads the bundled Inter font, whose metrics matter
#: at these widths.
CASES = tuple(
    (pixel_size, kind)
    for pixel_size in (13, 17, 20)
    for kind in ("direct-bpsr", "direct-strinova")
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app_text(qapp):
    """Set the desktop's text size, as ``theme.stylesheet()`` reads it."""
    theme.register_fonts()
    old_font = qapp.font()
    old_sheet = qapp.styleSheet()

    def use(pixel_size: int) -> None:
        font = QFont("Inter")
        font.setPixelSize(pixel_size)
        qapp.setFont(font)
        qapp.setStyleSheet(theme.stylesheet())

    try:
        yield use
    finally:
        qapp.setFont(old_font)
        qapp.setStyleSheet(old_sheet)


def _installing(page: GameView) -> None:
    page.set_sandbox(True, False)
    page.set_status(Status(state="not_installed", message="Installation incomplete"))
    page.set_busy("install")


def _install_page(
    qapp,
    kind: str,
    width: int,
    *,
    height: int = 900,
    state_before_show: bool = False,
    console: bool = False,
) -> GameView:
    page = GameView()
    page.set_profile("sample", {"backend": kind, "title": "Sample"})
    if state_before_show:
        _installing(page)
    page.resize(width, height)
    page.show()
    qapp.processEvents()
    if not state_before_show:
        _installing(page)
    if console:
        page._toggle_console()
        page.resize(width, height)
    _settle(qapp)
    return page


def _settle(qapp, rounds: int = 4) -> None:
    for _ in range(rounds):
        qapp.processEvents()


def _layout_problems(page: GameView) -> list[str]:
    hero = page.hero
    shown = {
        name: getattr(page, name)
        for name in ACTION_WIDGETS
        if not getattr(page, name).isHidden()
    }
    found: list[str] = []
    names = list(shown)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            if shown[first].geometry().intersects(shown[second].geometry()):
                found.append(f"{first} over {second}")
    rect = hero.rect()
    for name, widget in shown.items():
        if not rect.contains(widget.geometry()):
            found.append(f"{name} outside the hero ({widget.geometry()})")
    return found


def _height_problem(page: GameView, *, bloat: bool = False) -> str | None:
    hero = page.hero
    needed = hero.heightForWidth(hero.width())
    if hero.height() < needed:
        return f"hero is {hero.height()}px, its layout needs {needed}px"
    if bloat and hero.height() > needed + 2:
        return f"hero is {hero.height()}px, its layout only needs {needed}px"
    return None


def _close(page: GameView, qapp) -> None:
    page._timer.stop()
    page.deleteLater()
    _settle(qapp)


@pytest.mark.parametrize(("pixel_size", "kind"), CASES)
def test_install_page_actions_stay_inside_the_hero(qapp, app_text, pixel_size, kind):
    """With and without a notice, at every page width: nothing shares a row or is clipped."""
    app_text(pixel_size)
    failures: list[str] = []
    for width in WIDTHS:
        page = _install_page(qapp, kind, width)
        try:
            found = _layout_problems(page)
            if found:
                failures.append(f"{width}px: {', '.join(found)}")
        finally:
            _close(page, qapp)
    assert not failures, f"{kind} at {pixel_size}px text: " + "; ".join(failures)


@pytest.mark.parametrize(("pixel_size", "kind"), CASES)
def test_the_hero_reserves_the_height_its_layout_needs(qapp, app_text, pixel_size, kind):
    """The Maximum policy caps the hero at its hint: that hint must know the width."""
    app_text(pixel_size)
    for width in WIDTHS:
        page = _install_page(qapp, kind, width)
        try:
            problem = _height_problem(page)
            assert problem is None, f"{kind} at {pixel_size}px text, {width}px: {problem}"
        finally:
            _close(page, qapp)


@pytest.mark.parametrize(("pixel_size", "kind"), CASES)
def test_narrowing_a_shown_page_does_not_leave_the_actions_overlapping(
    qapp, app_text, pixel_size, kind
):
    """1280 -> 680 -> 1280 on a live page: no overlap, and no bloated card afterwards."""
    app_text(pixel_size)
    page = _install_page(qapp, kind, 1280)
    try:
        assert _layout_problems(page) == []
        page.resize(680, 900)
        _settle(qapp, 6)
        found = _layout_problems(page)
        assert not found, f"{kind} at {pixel_size}px text, 680px: {', '.join(found)}"
        problem = _height_problem(page)
        assert problem is None, f"{kind} at {pixel_size}px text, 680px: {problem}"
        page.resize(1280, 900)
        _settle(qapp, 6)
        found = _layout_problems(page)
        assert not found, f"{kind} at {pixel_size}px text, back at 1280px: {', '.join(found)}"
        problem = _height_problem(page, bloat=True)
        assert problem is None, f"{kind} at {pixel_size}px text, back at 1280px: {problem}"
    finally:
        _close(page, qapp)


@pytest.mark.parametrize(("pixel_size", "kind"), CASES)
def test_state_set_before_show_lays_out_at_the_real_width(
    qapp, app_text, pixel_size, kind
):
    """The installing state is often set before the first show; the width is 1280 then."""
    app_text(pixel_size)
    page = _install_page(qapp, kind, 1280, state_before_show=True)
    try:
        found = _layout_problems(page)
        assert not found, f"{kind} at {pixel_size}px text: {', '.join(found)}"
        problem = _height_problem(page, bloat=True)
        assert problem is None, f"{kind} at {pixel_size}px text: {problem}"
    finally:
        _close(page, qapp)


def test_a_short_page_with_the_output_open_keeps_the_actions_in_the_hero(qapp, app_text):
    """720px tall with OUTPUT open: the hero grows and the page scrolls, nothing overlaps."""
    app_text(17)
    page = _install_page(qapp, "direct-strinova", 900, height=720, console=True)
    try:
        assert page.console.isVisible()
        found = _layout_problems(page)
        assert not found, ", ".join(found)
    finally:
        _close(page, qapp)
