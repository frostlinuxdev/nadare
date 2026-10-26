# SPDX-License-Identifier: GPL-3.0-or-later
"""#526: the notices banner in the main window."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core import notices, updatecheck  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.gui import main_window  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402


def _window(tmp_path, monkeypatch, items, **config_values):
    monkeypatch.setattr(updatecheck, "check_latest", lambda: None)
    monkeypatch.setattr(notices, "load", lambda *a, **k: list(items))
    config = Config.load(tmp_path / "config.json")
    config.data.update(config_values)
    return MainWindow(config)


def _wait(qapp, predicate, seconds=3.0):
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _n(**over):
    base = {"id": "n1", "text": "Endfield installs are failing.", "severity": "warn"}
    base.update(over)
    return notices.parse({"notices": [base]}, url="https://idx.example/n.json")[0]


def test_an_applicable_notice_shows_its_text_as_plain_text(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, [_n(text="<b>bold</b> & stuff")])
    try:
        assert _wait(qapp, lambda: not window.notice_banner.isHidden())
        assert window.notice_banner.message_label.text() == "<b>bold</b> & stuff"
        assert window.notice_banner.action_button.isHidden()  # no link, no button
        assert not window.notice_banner.dismiss_button.isHidden()
    finally:
        _close(window, qapp)


def test_a_link_gives_a_learn_more_button_that_opens_it(tmp_path, qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", lambda url: opened.append(url))
    window = _window(
        tmp_path, monkeypatch, [_n(link="https://github.com/frostlinuxdev/nadare/issues/9")]
    )
    try:
        assert _wait(qapp, lambda: not window.notice_banner.isHidden())
        window.notice_banner.action_button.click()
        assert [u.toString() for u in opened] == [
            "https://github.com/frostlinuxdev/nadare/issues/9"
        ]
    finally:
        _close(window, qapp)


def test_dismissing_hides_it_and_remembers_the_id(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, [_n()])
    try:
        assert _wait(qapp, lambda: not window.notice_banner.isHidden())
        window.notice_banner.dismiss_button.click()
        assert window.notice_banner.isHidden()
        assert Config.load(tmp_path / "config.json").data["dismissed_notices"] == ["n1"]
    finally:
        _close(window, qapp)


def test_a_dismissed_notice_stays_quiet_next_start(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, [_n()], dismissed_notices=["n1"])
    try:
        qapp.processEvents()
        assert _wait(qapp, lambda: True)
        assert window.notice_banner.isHidden()
    finally:
        _close(window, qapp)


def test_a_block_notice_cannot_be_dismissed(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, [_n(severity="block")])
    try:
        assert _wait(qapp, lambda: not window.notice_banner.isHidden())
        assert window.notice_banner.dismiss_button.isHidden()
    finally:
        _close(window, qapp)


def test_a_notice_for_another_game_or_a_newer_version_is_not_shown(tmp_path, qapp, monkeypatch):
    items = [_n(id="a", kinds=["direct-endfield"]), _n(id="b", below="0.0.1")]
    window = _window(tmp_path, monkeypatch, items)
    try:
        assert _wait(qapp, lambda: True)
        qapp.processEvents()
        assert window.notice_banner.isHidden()
    finally:
        _close(window, qapp)


def test_a_notice_targeting_an_installed_game_is_shown(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("end", backend="direct-endfield", title="Endfield")
    config.save()
    monkeypatch.setattr(updatecheck, "check_latest", lambda: None)
    monkeypatch.setattr(notices, "load", lambda *a, **k: [_n(kinds=["direct-endfield"])])
    window = MainWindow(Config.load(tmp_path / "config.json"))
    try:
        assert _wait(qapp, lambda: not window.notice_banner.isHidden())
    finally:
        _close(window, qapp)


def test_the_setting_that_turns_off_update_checks_also_turns_off_notices(
    tmp_path, qapp, monkeypatch
):
    def boom(*a, **k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(notices, "load", boom)
    monkeypatch.setattr(updatecheck, "check_latest", boom)
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    window = MainWindow(config)
    try:
        qapp.processEvents()
        assert window.notice_banner.isHidden()
    finally:
        _close(window, qapp)


def test_the_update_banner_and_the_notice_banner_are_independent(tmp_path, qapp, monkeypatch):
    info = updatecheck.UpdateInfo("0.2.0", "https://forge.example/r/v0.2.0", "Nadare 0.2.0")
    monkeypatch.setattr(updatecheck, "check_latest", lambda: info)
    monkeypatch.setattr(notices, "load", lambda *a, **k: [_n()])
    window = MainWindow(Config.load(tmp_path / "config.json"))
    try:
        assert _wait(
            qapp,
            lambda: not window.notice_banner.isHidden() and not window.update_banner.isHidden(),
        )
        window.notice_banner.dismiss_button.click()
        assert window.notice_banner.isHidden() and not window.update_banner.isHidden()
    finally:
        _close(window, qapp)
