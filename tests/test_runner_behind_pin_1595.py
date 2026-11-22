# SPDX-License-Identifier: GPL-3.0-or-later
"""#1595: a game set to one runner build, or to a custom Wine inside one, says when it fell behind.

Profiles that follow the pin get the new build on their own (#1425). A profile that names one
build (or a ``wine_binary`` inside a runner tree) stays where it is; the game's Settings now say
so when the tested pin of that family is newer.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import runners


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    monkeypatch.setattr(
        runners, "pinned_tag", lambda family: {"dwproton": "dwproton-11.0-17"}.get(family)
    )
    return folder


def _make(root, name):
    d = root / name
    (d / "files" / "bin").mkdir(parents=True)
    (d / "proton").write_text("#!/bin/sh\n")
    (d / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    return d


@pytest.mark.parametrize(
    "profile",
    [
        {"backend": "wine", "runner": "dwproton-11.0-15-x86_64"},
        {"backend": "wine", "runner": "{root}/dwproton-11.0-15-x86_64"},
        {"backend": "wine", "extra": {"proton_runner": "{root}/dwproton-11.0-15-x86_64"}},
        {"backend": "wine", "wine_binary": "{root}/dwproton-11.0-15-x86_64/files/bin/wine"},
    ],
)
def test_a_named_older_build_is_behind(root, profile):
    _make(root, "dwproton-11.0-15-x86_64")
    profile = _fill(profile, root)
    assert runners.behind_pin(profile) == ("dwproton-11.0-15-x86_64", "dwproton-11.0-17")


@pytest.mark.parametrize(
    "profile",
    [
        {"backend": "wine"},  # follows the pin
        {"backend": "wine", "runner": "dwproton"},  # a family id follows the pin
        {"backend": "wine", "runner": "latest"},
        {"backend": "wine", "runner": "dwproton-11.0-17-x86_64"},  # the pin itself
        {"backend": "wine", "runner": "dwproton-11.0-18-x86_64"},  # newer: untested, not behind
        {"backend": "wine", "runner": "GE-Proton10-1"},  # a family with no pin
        {"backend": "wine", "runner": "my-own-proton"},  # custom: nothing to compare
        {"backend": "wine", "wine_binary": "/usr/bin/wine"},
        {"backend": "wine", "runner": "system"},
    ],
)
def test_nothing_to_say(root, profile):
    assert runners.behind_pin(profile) is None


def _fill(profile, root):
    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value.replace("{root}", str(root)) if isinstance(value, str) else value

    return fix(profile)


# ---- the game's Settings -----------------------------------------------------------------


def _page(qapp):
    from nadare.gui.settings import SettingsPage

    return SettingsPage()


@pytest.fixture(scope="module")
def qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable in this interpreter")
    app = QApplication.instance() or QApplication([])
    yield app


def test_settings_show_the_note_for_an_older_build_and_clear_it_on_default(root, tmp_path, qapp):
    from nadare.core.config import Config

    old = _make(root, "dwproton-11.0-15-x86_64")
    _make(root, "dwproton-11.0-17-x86_64")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("game", backend="wine", runner=str(old))
    page = _page(qapp)
    try:
        page.load(config, "game")
        text = page.runner_behind.text()
        assert not page.runner_behind.isHidden()
        assert "dwproton-11.0-15-x86_64" in text and "dwproton-11.0-17" in text

        page.runner.setCurrentIndex(page.runner.findData(""))
        assert page.runner_behind.isHidden()
    finally:
        page.deleteLater()


def test_settings_say_nothing_for_a_game_that_follows_the_pin(root, tmp_path, qapp):
    from nadare.core.config import Config

    _make(root, "dwproton-11.0-17-x86_64")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("game", backend="wine")
    page = _page(qapp)
    try:
        page.load(config, "game")
        assert page.runner_behind.isHidden()
    finally:
        page.deleteLater()
