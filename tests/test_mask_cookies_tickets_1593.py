# SPDX-License-Identifier: GPL-3.0-or-later
"""Logs and support reports never carry a cookie, a ticket or a session id (#1593 item 1).

Publisher sign-ins hand out cookies (STOVE, Netmarble), login tickets (HoYo ``login_ticket``,
Kuro) and session ids; a log line that quotes one is as good as the password. Both maskers
must catch them: :func:`events.mask` (files) and :func:`events.mask_known` (the log viewer),
because the viewer's mask leaves short values alone.
"""

from __future__ import annotations

import pytest

from nadare.core import events

SECRET = "zq"  # short on purpose: the length heuristic must not be what saves us
MASKERS = [events.mask, events.mask_known]


@pytest.mark.parametrize("masker", MASKERS)
@pytest.mark.parametrize(
    "line",
    [
        f"request headers: Cookie: SUID={SECRET}1; SNAME={SECRET}2; lang=en",
        f"Set-Cookie: SESSION={SECRET}1; Path=/; HttpOnly",
        f"cookie={SECRET}1",
        f'{{"cookie": "a={SECRET}1; b={SECRET}2", "ua": "x"}}',
        f"{{'Cookie': 'a={SECRET}1; b={SECRET}2'}}",
        f"login_ticket={SECRET}1",
        f"ticket: {SECRET}1",
        f'{{"ticket": "{SECRET}1"}}',
        f"PHPSESSID={SECRET}1",
        f"session_id={SECRET}1",
        f"sessionId: {SECRET}1",
        f"session-key={SECRET}1",
    ],
)
def test_cookie_ticket_and_session_values_are_masked(masker, line):
    out = masker(line)
    assert SECRET not in out, out


@pytest.mark.parametrize("masker", MASKERS)
def test_the_line_stays_readable_around_a_cookie(masker):
    out = masker(f'headers {{"cookie": "a={SECRET}1", "user-agent": "nadare"}}')
    assert "user-agent" in out and "nadare" in out


@pytest.mark.parametrize("masker", MASKERS)
@pytest.mark.parametrize(
    "line",
    [
        "cookies: 3 stored",
        "session started",
        "tickets left: none",
    ],
)
def test_ordinary_words_are_left_alone(masker, line):
    assert masker(line) == line


@pytest.mark.parametrize(
    "key", ["cookie", "Cookie", "login_ticket", "ticket", "session_id", "PHPSESSID"]
)
def test_settings_with_these_names_are_secret(key):
    assert events.is_secret_key(key)


def test_a_ticket_flag_value_is_masked():
    assert SECRET not in events.mask_known(f"game.exe --ticket {SECRET}1 -windowed")
