# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI audit round 7 regressions (split out of test_gui_smoke.py, #1248)."""

from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


# ---------------------------------------------------------------------------
# Audit round 7 regressions (G-3/G-4/G-7/G-8/G-9)
# ---------------------------------------------------------------------------
def test_busy_badge_reflects_the_operation_not_installing(qapp):
    """G-3: stop/uninstall/set_remember/logout must not show the INSTALLING pill."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {})
    try:
        for operation, label in (
            ("stop", "STOPPING"),
            ("uninstall", "UNINSTALLING"),
            ("set_remember", "SAVING"),
            ("logout", "LOGGING OUT"),
        ):
            view.set_busy(operation)
            assert view.badge.text() == label, operation
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_busy_badge_resets_after_fail_and_cancel(qapp, monkeypatch):
    """G-4: a failed or cancelled operation must drop the busy pill."""
    from nadare.core.backends import Status
    from nadare.gui import game_view as game_view_mod
    from nadare.gui.game_view import GameView

    monkeypatch.setattr(game_view_mod.QMessageBox, "warning", lambda *a, **k: None)
    view = GameView()
    view.set_profile("sample", {})
    view.set_status(Status("installed"))
    try:
        assert view.badge.text() == "READY"
        view.set_busy("install")
        assert view.badge.text() == "INSTALLING"
        view.fail("boom")
        assert view.badge.text() == "READY", "a failed op kept the busy pill"

        view.set_busy("launch")
        assert view.badge.text() == "PLAYING"
        view.cancelled()
        assert view.badge.text() == "READY", "a cancelled op kept the busy pill"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_remember_card_follows_the_profiles_backend(tmp_path, qapp):
    """G-7: the Account card shows for stove-czn and stays hidden for wine."""
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("winey", backend="wine")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "winey")
        assert page.account_card.isHidden(), "wine has no Remember me"
        page.load(config, "czn")
        assert not page.account_card.isHidden(), "stove-czn supports Remember me"
        page.load(config, "winey")
        assert page.account_card.isHidden()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_has_no_backend_picker(tmp_path, qapp):
    """The backend is fixed when a game is added; settings must not offer to change it."""
    from PyQt6.QtWidgets import QComboBox

    from nadare.core.backends import kinds
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "czn")
        assert not hasattr(page, "backend")
        offered = {
            combo.itemText(i)
            for combo in page.findChildren(QComboBox)
            for i in range(combo.count())
        }
        assert not offered & set(kinds()), "a backend kind is selectable in settings"
        page.save()
        assert config.profile("czn")["backend"] == "stove-czn"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_login_qr_is_restored_when_returning_to_a_busy_login(tmp_path, qapp, monkeypatch):
    """G-8: leaving and reopening a busy login must bring the QR back."""
    from PyQt6.QtGui import QPixmap

    from nadare.core.events import Event
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.add_profile("czn", backend="stove-czn", title="CZN")
    config.add_profile("other", backend="wine", title="Other")
    window = MainWindow(config, profile="czn")
    qr = tmp_path / "qr.png"
    pix = QPixmap(96, 96)
    pix.fill()
    assert pix.save(str(qr))
    try:
        window._busy["czn"] = "login"
        window._on_event("czn", Event("step", "Scan", data={"png": str(qr)}))
        window.show_library()
        window.open_game("czn")
        qapp.processEvents()
        assert not window.game.qr_frame.isHidden(), "the QR must survive a revisit"
        assert not window.game.qr_label.pixmap().isNull()

        window._on_event("czn", Event("done", "Logged in to STOVE"))
        window._busy.pop("czn", None)
        window.show_library()
        window.open_game("czn")
        qapp.processEvents()
        assert window.game.qr_frame.isHidden(), "a finished login must not restore a QR"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remember_notice_reaches_the_user_after_settings_save(tmp_path, qapp, monkeypatch):
    """G-9: the no-keyring notice must be shown, not dropped on a hidden page."""
    from nadare.backends.czn.backend import REMEMBER_NOTICE
    from nadare.core.events import Event
    from nadare.gui import main_window as mw
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.add_profile("czn", backend="stove-czn", title="CZN", remember_me=True)
    window = MainWindow(config)
    shown: list[tuple] = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: shown.append(a))
    try:
        window._on_settings_saved("czn")
        window._on_event("czn", Event("status", REMEMBER_NOTICE))
        window._on_succeeded("czn", "set_remember", 0)
        assert shown, "the no-keyring notice must reach the user"
        assert shown[0][2].startswith(REMEMBER_NOTICE)
        # With no keyring nothing can be saved, so Remember me is switched back off.
        assert "turned off" in shown[0][2]
        assert not config.profile("czn").get("remember_me", False)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_message_boxes_are_themed():
    """Dialogs must not fall back to the system's light palette on the dark app."""
    from nadare.gui.theme import PANEL, STYLESHEET

    assert "QMessageBox" in STYLESHEET
    assert f"background: {PANEL}" in STYLESHEET.split("QMessageBox", 1)[1][:200]


def test_remove_game_from_the_gui_drops_profile_and_keeps_files(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install))
    cfg.add_profile("h", backend="wine", title="H")
    cfg.save()
    window = MainWindow(cfg)
    try:
        # The settings page exposes the action and forwards the profile name.
        got = []
        window.settings.remove_requested.disconnect()  # no modal confirmation in the test
        window.settings.remove_requested.connect(got.append)
        window.settings.load(cfg, "g")
        window.settings.remove_button.click()
        assert got == ["g"]

        window._remove_game("g", False)
        assert _wait_for(qapp, lambda: "g" not in cfg.profiles)
        assert "h" in cfg.profiles
        assert (install / "game.exe").exists()
        assert "g" not in Config.load(tmp_path / "config.json").profiles
        assert "g" not in window._busy
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_game_can_delete_the_install_dir_from_the_gui(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    other = tmp_path / "games" / "h"
    other.mkdir()
    (other / "keep.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile(
        "g", backend="wine", title="G", install_dir=str(install), owned_trees=["install_dir"]
    )
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(other))
    cfg.save()
    window = MainWindow(cfg)
    try:
        window._remove_game("g", False, ["install_dir"])
        assert _wait_for(qapp, lambda: "g" not in cfg.profiles)
        assert not install.exists()
        assert (other / "keep.exe").exists()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_refused_tree_keeps_the_profile_in_the_gui(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    shared = tmp_path / "games" / "shared"
    shared.mkdir(parents=True)
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(shared))
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(shared))
    cfg.save()
    window = MainWindow(cfg)
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.warning", lambda *a, **k: None
    )
    try:
        window._remove_game("g", False, ["install_dir"])
        assert _wait_for(qapp, lambda: "g" not in window._busy)
        assert "g" in cfg.profiles and shared.exists()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_game_removed_drops_cached_snapshot(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        window._snapshots["g"] = {"news": object()}
        window._remember_notice["g"] = "x"
        window._remember_watching.add("g")
        window._on_game_removed("g", None)
        assert "g" not in window._snapshots
        assert "g" not in window._remember_notice
        assert "g" not in window._remember_watching
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def _confirm_window(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    shared = tmp_path / "games" / "shared"
    shared.mkdir(parents=True)
    cfg = _empty_config(tmp_path)
    cfg.add_profile(
        "g",
        backend="wine",
        title="G",
        install_dir=str(install),
        prefix=str(shared),
        owned_trees=["install_dir"],
    )
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(shared))
    cfg.save()
    window = MainWindow(cfg)
    calls = []
    window._remove_game = lambda *a, **k: calls.append(a)
    return window, calls, QMessageBox


def test_remove_dialog_cancel_does_nothing(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    from PyQt6.QtWidgets import QCheckBox

    seen = {}

    def fake_exec(self):
        seen["default"] = self.defaultButton()
        seen["boxes"] = {c.text(): c.isEnabled() for c in self.findChildren(QCheckBox)}
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("g")
        assert calls == []
        assert seen["default"] is not None
        enabled = [v for k, v in seen["boxes"].items() if "kept:" not in k and "install" in k]
        disabled = [v for k, v in seen["boxes"].items() if "kept:" in k]
        assert enabled == [True]  # install_dir is safe
        assert disabled == [False]  # prefix is shared with h: disabled, with the reason
        assert "g" in window.config.profiles
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_dialog_default_button_is_cancel(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    seen = {}

    def fake_exec(self):
        seen["default"] = self.defaultButton().text()
        seen["cancel"] = self.button(QMessageBox.StandardButton.Cancel).text()
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("g")
        assert seen["default"] == seen["cancel"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_dialog_second_confirmation_cancel_does_nothing(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    from PyQt6.QtWidgets import QCheckBox

    def fake_exec(self):
        for check in self.findChildren(QCheckBox):
            if check.isEnabled():
                check.setChecked(True)
        return QMessageBox.StandardButton.Yes

    asked = []

    def fake_warning(*args):
        asked.append(args[1])
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    try:
        window._on_remove_requested("g")
        assert asked == ["Delete game files?"]
        assert calls == []
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_profile_restored_on_save_failure(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G")
    cfg.add_profile("h", backend="wine", title="H")
    cfg.save()
    window = MainWindow(cfg)
    shown = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a: shown.append(a[2]))
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: None))
    monkeypatch.setattr(cfg, "save", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    try:
        active = cfg.active
        window._on_game_removed("g", None)
        assert "g" in cfg.profiles and "h" in cfg.profiles
        assert cfg.active == active
        assert shown and "ro" in shown[0]
        assert "Removed" not in window.side_status.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
