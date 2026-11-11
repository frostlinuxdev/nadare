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
    assert "sign in" in text and "downloads the rest of the game" in text
    assert "close the launcher window to finish" not in text


def test_the_notice_keeps_the_anti_cheat_warning_and_the_official_link():
    notice = backends.page_notice("launcher-nikke")
    # NIKKE is Verified (decision 2026-11-08): the risk is said, without "experimental".
    assert "ACE" in notice[0] and "anti-cheat" in notice[0]
    assert notice[1] == "https://nikke-en.com/"


def test_the_notice_says_the_install_needs_no_window_and_that_play_opens_the_launcher():
    text = _notice()
    assert "without opening any window" in text
    assert "Press Play" in text
    assert text.index("without opening any window") < text.index("miniloader")


def test_the_notice_says_closing_the_window_only_hides_the_launcher():
    text = _notice()
    assert "tray" in text and "x next to PLAYING" in text


def test_the_notice_names_the_launchers_own_exit_choice():
    """Measured 2026-11-10, signed in: closing the window asks "When Closing The Launcher" with
    "Exit launcher" and "Minimize to taskbar instead of exiting" (the default)."""
    text = _notice()
    assert "Exit launcher" in text and "Save selection" in text
    assert "only hides" not in text


def test_the_summary_and_the_option_do_not_say_closing_always_hides_it():
    from nadare.core.backends import get

    backend = get("launcher-nikke")
    assert "only hides" not in backend.page_summary
    option = next(o for o in backend.options if o.key == "stop_when_hidden")
    assert "only hides" not in option.help and "tray" in option.help


def test_the_notice_says_what_to_do_when_the_captcha_window_is_blank():
    """#1124: on a Wayland desktop the Level Infinite captcha opened blank; X11 worked."""
    text = _notice()
    assert "captcha" in text and "blank" in text
    assert "X11" in text and "Display backend" in text


def test_the_notice_is_short_and_names_the_way_without_the_launcher():
    """Maintainer, 2026-11-11: the page text was a wall of text and only told the launcher route."""
    text = _notice()
    assert len(text) <= 820, len(text)  # was about 1100
    assert "Log in with Google" in text and "without the NIKKE launcher" in text
