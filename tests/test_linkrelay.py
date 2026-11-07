# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure mode: a game asks to open a link; Nadare opens it in the browser, nothing else (#804)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nadare.core import gamesandbox, linkrelay
from nadare.core.linkrelay import LinkRelay, parse_request

STEAM_LINE = "'~/.steam/root/ubuntu12_32/steam' 'steam://openurl_external/{url}'\n"


# ---- what the game may ask for -----------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.example.com/login?return=game",
        "http://localhost:8080/callback",
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&state=a%20b",
        "https://[::1]:8080/callback",
        "https://[2001:db8::1]/x",
        "http://127.0.0.1:53682/?code=abc#frag",
        "https://login.example.co.uk:8443/a/b;c=d/e?x=1&y=2#z",
    ],
)
def test_a_web_link_is_accepted(url):
    assert parse_request(STEAM_LINE.format(url=url)) == url


def test_the_plain_steam_forms_are_accepted_too():
    assert parse_request("steam://openurl_external/https://a.example/x") == "https://a.example/x"
    assert parse_request("steam://openurl/https://a.example/x") == "https://a.example/x"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello",
        STEAM_LINE.format(url="file:///home/user/.ssh/id_ed25519"),
        STEAM_LINE.format(url="javascript:alert(1)"),
        STEAM_LINE.format(url="ftp://example.com/x"),
        STEAM_LINE.format(url="https://"),
        STEAM_LINE.format(url="https:///nohost"),
        STEAM_LINE.format(url="https://example.com/a b"),
        STEAM_LINE.format(url="https://example.com/\x1b[31m"),
        "'steam' 'steam://run/12345'\n",
        "'steam' 'steam://rungameid/12345'\n",
        "'steam' 'steam://install/12345'\n",
        STEAM_LINE.format(url="https://example.com/" + "a" * 5000),
        # found by fuzzing the parser
        STEAM_LINE.format(url="https://evil.com\\@good.com/"),  # browsers read the \\ as a /
        STEAM_LINE.format(url="https://:80/x"),  # no host
        "steam://openurl_external/https://[::1]'\n",  # a stray quote after the address
        STEAM_LINE.format(url="https://accouns\u3002examp.com/login"),  # looks like a dot
        STEAM_LINE.format(url="https://exa\u200bmple.com/"),  # zero-width character
        STEAM_LINE.format(url="https://caf\u00e9.example/"),  # non-ASCII must be encoded first
        STEAM_LINE.format(url="https://google.com@evil.example/"),  # who is the host?
        STEAM_LINE.format(url="https://user:pw@host.example/"),
        STEAM_LINE.format(url="https://a.com%00.evil.example/"),
        STEAM_LINE.format(url="https://a.com:99999/"),
        STEAM_LINE.format(url="https://a.com:0/"),
        STEAM_LINE.format(url="https://a.com:http/"),
        STEAM_LINE.format(url="https://-/"),
        STEAM_LINE.format(url="https://../"),
        STEAM_LINE.format(url="https://a.com/x\"y"),
        STEAM_LINE.format(url="https://a.com/<script>"),
        STEAM_LINE.format(url="https://a.com/`id`"),
        STEAM_LINE.format(url="https://0x7f.1/"),
    ],
)
def test_anything_else_is_refused(line):
    assert parse_request(line) is None


def test_a_link_cannot_smuggle_a_second_command():
    line = STEAM_LINE.format(url="https://a.example/x") + "'steam' 'steam://run/1'\n"
    assert parse_request(line.splitlines()[0]) == "https://a.example/x"
    assert parse_request(line.splitlines()[1]) is None


# ---- the relay: a pipe that Nadare reads -------------------------------------------------
@pytest.fixture
def relay(tmp_path):
    opened: list[str] = []
    r = LinkRelay(tmp_path, opener=opened.append, min_gap=0.0)
    path = r.start()
    yield r, path, opened
    r.stop()


def _send(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _wait(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_relay_opens_a_link_the_game_asks_for(relay):
    _r, path, opened = relay
    _send(path, STEAM_LINE.format(url="https://accounts.example.com/login"))
    assert _wait(lambda: opened == ["https://accounts.example.com/login"])


def test_the_pipe_is_private_to_the_user(relay):
    _r, path, _opened = relay
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_refused_requests_open_nothing(relay):
    _r, path, opened = relay
    _send(path, STEAM_LINE.format(url="file:///etc/passwd"))
    _send(path, "'steam' 'steam://run/12345'\n")
    _send(path, STEAM_LINE.format(url="https://ok.example/"))
    assert _wait(lambda: opened == ["https://ok.example/"])
    assert opened == ["https://ok.example/"]


def test_a_game_cannot_flood_the_browser(tmp_path):
    opened: list[str] = []
    r = LinkRelay(tmp_path, opener=opened.append, min_gap=0.0, max_links=3)
    path = r.start()
    try:
        for i in range(10):
            _send(path, STEAM_LINE.format(url=f"https://a.example/{i}"))
        assert _wait(lambda: len(opened) == 3)
        time.sleep(0.3)
        assert len(opened) == 3
    finally:
        r.stop()


def test_the_same_link_twice_in_a_row_opens_once(relay):
    _r, path, opened = relay
    for _ in range(3):
        _send(path, STEAM_LINE.format(url="https://same.example/"))
    assert _wait(lambda: opened == ["https://same.example/"])
    time.sleep(0.3)
    assert opened == ["https://same.example/"]


def test_a_gap_is_kept_between_two_different_links(tmp_path):
    opened: list[str] = []
    r = LinkRelay(tmp_path, opener=opened.append, min_gap=0.4)
    path = r.start()
    try:
        _send(path, STEAM_LINE.format(url="https://a.example/1"))
        _send(path, STEAM_LINE.format(url="https://a.example/2"))
        assert _wait(lambda: len(opened) == 1)
        time.sleep(0.15)
        assert len(opened) == 1  # the second one came too soon and was dropped
    finally:
        r.stop()


def test_stop_removes_the_pipe_and_its_folder(tmp_path):
    r = LinkRelay(tmp_path, opener=lambda url: None)
    path = r.start()
    assert path.exists()
    r.stop()
    assert not path.exists() and not path.parent.exists()
    r.stop()  # stopping twice is harmless


def test_a_failing_opener_does_not_kill_the_relay(tmp_path):
    seen: list[str] = []

    def opener(url):
        seen.append(url)
        if len(seen) == 1:
            raise OSError("no browser")

    r = LinkRelay(tmp_path, opener=opener, min_gap=0.0)
    path = r.start()
    try:
        _send(path, STEAM_LINE.format(url="https://a.example/1"))
        _send(path, STEAM_LINE.format(url="https://a.example/2"))
        assert _wait(lambda: len(seen) == 2)
    finally:
        r.stop()


def test_the_default_opener_uses_xdg_open_with_a_clean_environment(monkeypatch):
    calls = {}

    class Done:
        pass

    def fake_popen(args, **kwargs):
        calls["args"], calls["kwargs"] = args, kwargs
        return Done()

    monkeypatch.setattr(linkrelay.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(linkrelay.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    linkrelay.open_in_browser("https://a.example/x")
    assert calls["args"] == ["/usr/bin/xdg-open", "https://a.example/x"]
    assert "GITHUB_TOKEN" not in calls["kwargs"]["env"]


def test_without_xdg_open_nothing_is_started(monkeypatch):
    monkeypatch.setattr(linkrelay.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        linkrelay.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start anything")
    )
    assert linkrelay.open_in_browser("https://a.example/x") is False


# ---- the sandbox binds it where the Steam Runtime looks for the Steam client's pipe -----
def test_wrap_binds_the_pipe_where_the_steam_client_pipe_would_be(tmp_path):
    home = tmp_path / "home" / "u"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    pipe = tmp_path / "relay" / "steam.pipe"
    pipe.parent.mkdir()
    os.mkfifo(pipe)
    argv, _env = gamesandbox.wrap(
        ["x"], {"HOME": str(home)}, prefix=prefix, home=home, link_pipe=pipe
    )
    flags = argv[1 : argv.index("--")]
    pairs = [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == "--bind"]
    assert (str(pipe), str(home.resolve() / ".steam" / "steam.pipe")) in pairs


def test_without_a_pipe_nothing_is_bound_there(tmp_path):
    home = tmp_path / "home" / "u"
    prefix = home / "prefix"
    prefix.mkdir(parents=True)
    argv, _env = gamesandbox.wrap(["x"], {"HOME": str(home)}, prefix=prefix, home=home)
    assert ".steam" not in " ".join(argv)
