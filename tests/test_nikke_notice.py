# SPDX-License-Identifier: GPL-3.0-or-later
"""The NIKKE page notice describes the flow the miniloader really has."""

from __future__ import annotations

from nadare.core import backends


def _notice() -> str:
    return backends.page_notice("launcher-nikke")[0]


def test_the_notice_says_install_then_launch_not_install_twice():
    text = _notice()
    assert "INSTALL" in text and "LAUNCH" in text
    assert "INSTALL again" not in text
    assert text.index("INSTALL") < text.index("LAUNCH")


def test_the_notice_says_where_the_game_comes_from_and_how_the_install_ends():
    text = _notice().lower()
    assert "miniloader" in text
    assert "sign in" in text and "downloads the game" in text
    assert "close the launcher window to finish" not in text


def test_the_notice_keeps_the_anti_cheat_warning_and_the_official_link():
    notice = backends.page_notice("launcher-nikke")
    assert "ACE" in notice[0] and "experimental" in notice[0]
    assert notice[1] == "https://nikke-en.com/"


def test_the_notice_says_the_install_needs_no_window_and_that_play_opens_the_launcher():
    text = _notice()
    assert "without opening any window" in text
    assert "Press Play" in text
    assert text.index("without opening any window") < text.index("miniloader")


def test_the_notice_says_closing_the_window_only_hides_the_launcher():
    text = _notice()
    assert "tray" in text and "press STOP" in text
