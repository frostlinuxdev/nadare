# SPDX-License-Identifier: GPL-3.0-or-later
"""A seeded fuzzer for the link relay's parser: what it accepts must always be a safe web link.

The parser reads text written by an untrusted game. A longer run (``python -c`` with a bigger
count, several seeds) found lookalike-dot hosts, ``\\@`` host confusion, missing hosts and stray
quotes; those are fixed and pinned in ``test_linkrelay.py``. This keeps the properties checked.
"""

from __future__ import annotations

import random
import string
from urllib.parse import urlsplit

from nadare.core.linkrelay import MAX_URL, parse_request

PREFIXES = [
    "steam://openurl_external/",
    "steam://openurl/",
    "STEAM://openurl_external/",
    "steam://run/",
    "steam:/openurl/",
    "steam://openurl_external//",
    "",
]
SCHEMES = [
    "http", "https", "HTTP", "HtTpS", "file", "javascript", "data", "ftp", "steam",
    "http ", " https", "ht\ttp", "https\n", "https ", "",
]  # fmt: skip
HOSTS = [
    "example.com", "localhost", "127.0.0.1", "[::1]", "", "@", "a@b", "user:pw@host", ":80",
    "host:99999", "ex ample.com", "exa\x00mple.com", "例え.jp", "evil.com\\@good.com",
    "good.com#@evil.com", "a.com%00.evil", "。", "..", "-", "a" * 300, "exa​mple.com",
    "\x7f", "0x7f.1", "2130706433", "%2e%2e", "1.2.3.4:80@5.6.7.8", "[::1", "[zz]", "[::1%25x]",
]  # fmt: skip
TAILS = [
    "", "/", "/x", "/x?y=1", "/x#f", "?q", "#f", "/a b", "/\x1b[31m", "/\n", "/'", '/"', "/\\",
    "/%00", "/ ", "/" + "a" * 3000, "/\t", "/\x7f", "/ ", "/ ", "/<b>", "/`id`",
]  # fmt: skip
WRAPS = [
    "'~/.steam/root/ubuntu12_32/steam' '{}'", "'{}'", "{}", "'a' '{}' 'b'", '"{}"',
    "'steam' '{}\n'", "'{}", "{}'",
]  # fmt: skip


def _generate(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.6:
        url = rng.choice(SCHEMES) + "://" + rng.choice(HOSTS) + rng.choice(TAILS)
        return rng.choice(WRAPS).format(rng.choice(PREFIXES) + url)
    if kind < 0.85:
        chars = [0, 9, 10, 13, 27, 32, 39, 34, 92, 127, 0xA0, 0x2028, 0x3002, 0x200B, 0x1F600]
        line = list("'x' 'steam://openurl_external/https://accounts.example.com/login?a=1'")
        for _ in range(rng.randint(1, 6)):
            i = rng.randrange(len(line))
            line.insert(i, chr(rng.choice(chars))) if rng.random() < 0.5 else line.pop(i)
        return "".join(line)
    return "".join(chr(rng.randrange(0, 0x3000)) for _ in range(rng.randint(0, 200)))


def _problems(url: str) -> list[str]:
    found = []
    if not url.startswith(("http://", "https://")):
        found.append("scheme")
    if len(url) > MAX_URL:
        found.append("too long")
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in url):
        found.append("space, control or non-ASCII character")
    if "\\" in url or any(c in url for c in "'\"<>`"):
        found.append("backslash or quote")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        found.append("the URL parser rejects it")
        return found
    if not parts.hostname:
        found.append("no host")
    if "@" in parts.netloc:
        found.append("user name in the link")
    return found


def test_whatever_the_parser_accepts_is_a_plain_web_link():
    accepted = 0
    for seed in (1, 2, 3):
        rng = random.Random(seed)
        for _ in range(12000):
            line = _generate(rng)
            url = parse_request(line)  # must never raise
            if url is None:
                continue
            accepted += 1
            assert not _problems(url), (line[:100], url[:100], _problems(url))
    assert accepted > 100, "the generator no longer produces valid links: the test proves nothing"


def test_random_text_never_raises():
    rng = random.Random(7)
    for _ in range(3000):
        text = "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 6000)))
        assert parse_request(text) is None or parse_request(text).startswith("http")
