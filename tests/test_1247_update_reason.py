# SPDX-License-Identifier: GPL-3.0-or-later
"""#1247: when Nadare can only offer Download, the banner says why and what to do."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import selfupdate
from nadare.core.updatecheck import UpdateInfo

GH = UpdateInfo(
    "0.1.0b23", "https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b23", "Nadare"
)


def _appimage(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Nadare-0.1.0b22-x86_64.AppImage"
    path.write_bytes(b"old")
    path.chmod(0o755)
    return path


# ---- the reason, in core --------------------------------------------------------------------


def test_a_replaceable_appimage_has_no_reason(tmp_path):
    path = _appimage(tmp_path / "apps")
    assert selfupdate.why_not_replaceable({"APPIMAGE": str(path)}) == ""
    assert selfupdate.appimage_target({"APPIMAGE": str(path)}) == path


def test_not_running_as_an_appimage_is_named(tmp_path):
    assert selfupdate.why_not_replaceable({}) == selfupdate.NOT_APPIMAGE
    plain = tmp_path / "nadare"
    plain.write_bytes(b"x")
    assert selfupdate.why_not_replaceable({"APPIMAGE": str(plain)}) == selfupdate.NOT_APPIMAGE


def test_a_package_managed_build_is_named(tmp_path):
    path = _appimage(tmp_path / "apps")
    env = {"APPIMAGE": str(path), "NADARE_MANAGED_UPDATES": "1"}
    assert selfupdate.why_not_replaceable(env) == selfupdate.MANAGED
    assert selfupdate.appimage_target(env) is None


def test_a_folder_the_user_cannot_write_is_named(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    folder = tmp_path / "ro"
    path = _appimage(folder)
    folder.chmod(0o555)
    try:
        env = {"APPIMAGE": str(path)}
        assert selfupdate.why_not_replaceable(env) == selfupdate.NOT_WRITABLE
        assert selfupdate.appimage_target(env) is None
    finally:
        folder.chmod(0o755)


def test_a_folder_others_can_change_is_named(tmp_path):
    folder = tmp_path / "shared"
    path = _appimage(folder)
    folder.chmod(0o777)  # world-writable without the sticky bit
    try:
        env = {"APPIMAGE": str(path)}
        assert selfupdate.why_not_replaceable(env) == selfupdate.SHARED_FOLDER
        assert selfupdate.appimage_target(env) is None
    finally:
        folder.chmod(0o755)


# ---- the words ------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [selfupdate.NOT_WRITABLE, selfupdate.SHARED_FOLDER])
def test_a_folder_problem_names_the_folder_and_both_fixes(code, tmp_path):
    path = tmp_path / "opt" / "Nadare.AppImage"
    short, detail = selfupdate.download_only_reason(code, path)
    assert short and detail
    assert str(path.parent) in detail
    assert "~/Applications" in detail
    assert "Gear Lever" in detail and "AppImageUpdate" in detail


def test_not_an_appimage_points_at_the_release_page():
    short, detail = selfupdate.download_only_reason(selfupdate.NOT_APPIMAGE, None)
    assert "AppImage" in short
    assert "release page" in detail


def test_the_managed_case_says_the_package_manager_updates_it():
    short, detail = selfupdate.download_only_reason(selfupdate.MANAGED, None)
    assert "package manager" in (short + detail)
    assert "Gear Lever" not in detail


def test_a_release_without_an_appimage_is_explained():
    short, detail = selfupdate.download_only_reason(selfupdate.NO_ASSET, None)
    assert short and "release page" in detail


def test_no_reason_means_no_words():
    assert selfupdate.download_only_reason("", None) == ("", "")


# ---- the banner -----------------------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core import updatecheck  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402


def _window(tmp_path, monkeypatch, info=GH):
    monkeypatch.setattr(updatecheck, "check_latest", lambda: info)
    monkeypatch.setattr(updatecheck, "managed_updates", lambda: False)
    return MainWindow(Config.load(tmp_path / "config.json"))


def _shown(window, qapp):
    import time

    end = time.monotonic() + 3
    while time.monotonic() < end and window.update_banner.isHidden():
        qapp.processEvents()
        time.sleep(0.01)
    return not window.update_banner.isHidden()


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_download_banner_says_why_and_what_to_do(tmp_path, qapp, monkeypatch):
    folder = tmp_path / "ro"
    path = _appimage(folder)
    monkeypatch.setenv("APPIMAGE", str(path))
    monkeypatch.setattr(selfupdate, "why_not_replaceable", lambda env=None: selfupdate.NOT_WRITABLE)
    window = _window(tmp_path, monkeypatch)
    try:
        assert _shown(window, qapp)
        banner = window.update_banner
        assert banner.action_button.text() == "Download"
        short, detail = selfupdate.download_only_reason(selfupdate.NOT_WRITABLE, path)
        assert short in banner.message_label.text()
        assert "Gear Lever" in banner.message_label.toolTip()
        assert "Gear Lever" in banner.action_button.toolTip()
    finally:
        _close(window, qapp)


def test_an_updatable_appimage_shows_update_without_a_reason(tmp_path, qapp, monkeypatch):
    path = _appimage(tmp_path / "apps")
    monkeypatch.setenv("APPIMAGE", str(path))
    window = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(window._self_update, "can_update", lambda info: True)
    # Even with a reason on hand, an Update banner must not carry one.
    monkeypatch.setattr(selfupdate, "why_not_replaceable", lambda env=None: selfupdate.NOT_WRITABLE)
    try:
        window._show_update(GH)
        banner = window.update_banner
        assert banner.action_button.text() == "Update"
        assert banner.message_label.toolTip() == ""
        assert "Gear Lever" not in banner.message_label.text()
    finally:
        _close(window, qapp)


def test_a_failed_update_falls_back_to_download_with_the_reason_cleared(
    tmp_path, qapp, monkeypatch
):
    path = _appimage(tmp_path / "apps")
    monkeypatch.setenv("APPIMAGE", str(path))
    window = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(window, "_show_update_failure", lambda message: None)
    try:
        window._show_update(GH)
        window._set_update_tip("an earlier reason")
        window._on_update_failed("checksum mismatch")
        banner = window.update_banner
        assert banner.action_button.text() == "Download"
        assert "could not update itself" in banner.message_label.text()
        assert banner.message_label.toolTip() == ""
        assert banner.action_button.toolTip() == ""
    finally:
        _close(window, qapp)
