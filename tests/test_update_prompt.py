# SPDX-License-Identifier: GPL-3.0-or-later
"""#525: a newer release gets a visible, dismissible banner, not only a sidebar button."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core import updatecheck  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.gui import main_window  # noqa: E402
from nadare.gui.banner import InfoBanner  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402

INFO = updatecheck.UpdateInfo("0.2.0", "https://forge.example/r/v0.2.0", "Nadare 0.2.0")


def _window(tmp_path, monkeypatch, info=INFO, **config_values):
    monkeypatch.setattr(updatecheck, "check_latest", lambda: info)
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


def test_banner_widget_signals_and_plain_text(qapp):
    banner = InfoBanner()
    seen = []
    banner.action_clicked.connect(lambda: seen.append("action"))
    banner.dismissed.connect(lambda: seen.append("later"))
    banner.show_message("<b>hi</b> & more", "Download", dismissible=True)
    assert banner.message_label.text() == "<b>hi</b> & more"  # never interpreted as rich text
    banner.action_button.click()
    banner.dismiss_button.click()
    assert seen == ["action", "later"]
    assert banner.dismiss_button.isHidden() is False
    banner.show_message("no way out", "Open", dismissible=False)
    assert banner.dismiss_button.isHidden()
    banner.hide_banner()
    assert banner.isHidden()


def test_a_new_release_shows_the_banner_with_both_versions(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        assert _wait(qapp, lambda: not window.update_banner.isHidden())
        text = window.update_banner.message_label.text()
        assert "v0.2.0" in text and "Nadare" in text
        assert not window.update_button.isHidden()  # the sidebar button stays too
    finally:
        _close(window, qapp)


def test_the_banner_button_opens_the_release_page(tmp_path, qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", lambda url: opened.append(url))
    window = _window(tmp_path, monkeypatch)
    try:
        assert _wait(qapp, lambda: not window.update_banner.isHidden())
        window.update_banner.action_button.click()
        assert [u.toString() for u in opened] == ["https://forge.example/r/v0.2.0"]
    finally:
        _close(window, qapp)


def test_later_hides_the_banner_and_remembers_that_version(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        assert _wait(qapp, lambda: not window.update_banner.isHidden())
        window.update_banner.dismiss_button.click()
        assert window.update_banner.isHidden()
        assert not window.update_button.isHidden()
        assert Config.load(tmp_path / "config.json").data["dismissed_update"] == "0.2.0"
    finally:
        _close(window, qapp)


def test_a_dismissed_version_stays_quiet_on_the_next_start(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, dismissed_update="0.2.0")
    try:
        assert _wait(qapp, lambda: not window.update_button.isHidden())
        assert window.update_banner.isHidden()
    finally:
        _close(window, qapp)


def test_a_still_newer_release_speaks_up_again(tmp_path, qapp, monkeypatch):
    newer = updatecheck.UpdateInfo("0.3.0", "https://forge.example/r/v0.3.0", "Nadare 0.3.0")
    window = _window(tmp_path, monkeypatch, info=newer, dismissed_update="0.2.0")
    try:
        assert _wait(qapp, lambda: not window.update_banner.isHidden())
        assert "v0.3.0" in window.update_banner.message_label.text()
    finally:
        _close(window, qapp)


def test_no_banner_when_there_is_no_update_or_checks_are_off(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch, info=None)
    try:
        qapp.processEvents()
        assert window.update_banner.isHidden()
    finally:
        _close(window, qapp)

    def boom():
        raise AssertionError("must not check")

    monkeypatch.setattr(updatecheck, "check_latest", boom)
    config = Config.load(tmp_path / "other.json")
    config.data["check_updates"] = False
    window = MainWindow(config)
    try:
        qapp.processEvents()
        assert window.update_banner.isHidden()
    finally:
        _close(window, qapp)


def test_a_non_https_release_link_is_never_opened(tmp_path, qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", lambda url: opened.append(url))
    bad = updatecheck.UpdateInfo("0.2.0", "http://forge.example/r", "x")
    window = _window(tmp_path, monkeypatch, info=bad)
    try:
        assert _wait(qapp, lambda: not window.update_banner.isHidden())
        window.update_banner.action_button.click()
        assert opened == []
    finally:
        _close(window, qapp)
