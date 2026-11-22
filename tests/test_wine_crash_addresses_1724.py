# SPDX-License-Identifier: GPL-3.0-or-later
"""Wine's crash lines keep their hex addresses; they are not secrets (#1724)."""

from __future__ import annotations

import pytest

from nadare.core import events

CRASH_LINES = [
    "wine: Unhandled page fault on read access to 000000000000036E at address "
    "00006FFFFFC174D7 (thread 1791c), starting debugger...",
    "wine: Unhandled page fault on write access to 00000000DEADBEEF at address "
    "0000000140001234 (thread 0024), starting debugger...",
    "wine: Unhandled exception 0xc0000005 in thread 1791c at address 00006FFFFFC174D7 "
    "(thread 1791c), starting debugger...",
    "Unhandled exception: page fault on read access to 0x000000000000036e in 64-bit code "
    "(0x00006fffffc174d7).",
    "=>0 0x00006fffffc174d7 in unityplayer (+0x1174d7) (0x000000000011f8a0)",
    "  1 0x0000000140001234 in game (+0x1234) (0x000000000011f9c0)",
    " rip:00006fffffc174d7 rsp:000000000011f6f0 rbp:000000000011f7a0 eflags:00010246",
    "PE\t0000000140000000-000000014000e000\tDeferred        game",
    "0x00006fffffc174d7 unityplayer+0x1174d7: movl\t0x0(%rax),%eax",
]


@pytest.mark.parametrize("line", CRASH_LINES)
def test_a_wine_crash_line_is_left_whole(line):
    assert events.mask(line) == line


def test_the_same_address_outside_a_crash_line_is_still_masked():
    out = events.mask("session 00006FFFFFC174D7 started")
    assert "00006FFFFFC174D7" not in out


def test_a_non_hex_secret_on_a_crash_line_is_still_masked():
    secret = "aB3dE5fG7hJ9kL1mQ2"
    out = events.mask(
        f"wine: Unhandled page fault on read access to 000000000000036E at address "
        f"00006FFFFFC174D7 (thread {secret}), starting debugger..."
    )
    assert secret not in out
    assert "000000000000036E" in out and "00006FFFFFC174D7" in out


def test_a_named_secret_on_a_crash_line_is_still_masked():
    out = events.mask("=>0 0x00006fffffc174d7 in game (+0x10) token=0123456789abcdef0123")
    assert "0123456789abcdef0123" not in out


def test_only_the_crash_line_of_a_multiline_text_keeps_its_hex():
    text = (
        "key 00006FFFFFC174D7AB\n"
        "wine: Unhandled page fault on read access to 000000000000036E at address "
        "00006FFFFFC174D7 (thread 1791c), starting debugger...\n"
        "after 0123456789ABCDEF"
    )
    out = events.mask(text).split("\n")
    assert "00006FFFFFC174D7AB" not in out[0]
    assert out[1] == text.split("\n")[1]
    assert "0123456789ABCDEF" not in out[2]
