# SPDX-License-Identifier: GPL-3.0-or-later
"""The Discord invite: read from the index when it looks like a Discord invite, else built in."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import community, indexfetch

GOOD = "https://discord.gg/9WkaSgJBft"


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.gg/9WkaSgJBft",
        "https://discord.com/invite/9WkaSgJBft",
        "https://discord.gg/some-name_1",
    ],
)
def test_real_invite_links_are_accepted(url):
    assert community.is_discord_invite(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        42,
        "http://discord.gg/9WkaSgJBft",
        "https://discord.gg.evil.example/9WkaSgJBft",
        "https://evil.example/discord.gg/9WkaSgJBft",
        "https://discord.gg@evil.example/9WkaSgJBft",
        "https://discord.gg:8443/9WkaSgJBft",
        "https://discord.gg/9WkaSgJBft?next=https://evil.example",
        "https://discord.gg/9WkaSgJBft#x",
        "https://discord.gg/",
        "https://discord.gg/a/b",
        "https://discord.gg/" + "a" * 40,
        "https://discord.com/other/9WkaSgJBft",
        "https://notdiscord.gg/9WkaSgJBft",
        "javascript:alert(1)",
        "file:///etc/passwd",
        " https://discord.gg/9WkaSgJBft",
        "https://discord.gg/9WkaSgJBft\\n",
    ],
)
def test_anything_else_is_refused(url):
    assert not community.is_discord_invite(url)


def _index(monkeypatch, payload):
    def fake_fetch(rel, *, validate=None, **kwargs):
        assert rel == community.LINKS_DOC
        if isinstance(payload, Exception):
            raise payload
        assert validate is None or validate(payload) in (True, None, payload)
        return indexfetch.IndexResult(payload, False, "https://index.example/")

    monkeypatch.setattr(indexfetch, "fetch_json", fake_fetch)


def test_the_index_value_wins_when_it_is_a_discord_invite(monkeypatch):
    _index(monkeypatch, {"discord": "https://discord.gg/NewInvite1"})
    assert community.discord_url() == "https://discord.gg/NewInvite1"


@pytest.mark.parametrize(
    "payload",
    [
        {"discord": "https://evil.example/x"},
        {"discord": "http://discord.gg/abc"},
        {"discord": ""},
        {"discord": 5},
        {"other": GOOD},
        {},
        [],
        "text",
        None,
        indexfetch.IndexUnavailable("down"),
        RuntimeError("anything"),
    ],
)
def test_a_bad_or_missing_index_entry_falls_back_to_the_built_in_invite(monkeypatch, payload):
    _index(monkeypatch, payload)
    assert community.discord_url() == GOOD


def test_the_built_in_invite_is_itself_valid():
    assert community.is_discord_invite(community.DISCORD_FALLBACK)
    assert community.DISCORD_FALLBACK == GOOD


# --- the button in Settings ---------------------------------------------------------------
try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    QApplication = None


@pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")
@pytest.mark.parametrize(
    ("answer", "opened"),
    [
        (GOOD, GOOD),
        ("https://discord.gg/NewInvite1", "https://discord.gg/NewInvite1"),
        ("https://evil.example/phish", GOOD),
        (None, GOOD),
    ],
)
def test_the_discord_button_opens_only_a_real_invite(monkeypatch, answer, opened):
    from nadare.gui import settings as settings_mod

    app = QApplication.instance() or QApplication([])
    seen: list[str] = []
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: seen.append(url.toString())),
    )
    monkeypatch.setattr(settings_mod.community, "discord_url", lambda: answer)
    page = settings_mod.SettingsPage()
    try:
        page.discord_button.click()
        assert not page.discord_button.isEnabled()  # one lookup at a time
        QThreadPool.globalInstance().waitForDone()
        app.processEvents()
        QThreadPool.globalInstance().waitForDone()
        app.processEvents()
        assert seen == [opened]
        assert page.discord_button.isEnabled()
    finally:
        page.deleteLater()
        app.processEvents()
