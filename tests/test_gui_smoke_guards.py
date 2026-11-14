# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI smoke: update opt-out, remove confirmations, notices, keyboard and close warnings (split out
of test_gui_smoke.py, #1248)."""

from __future__ import annotations

import time  # noqa: E402

import pytest
from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)
from PyQt6.QtWidgets import QCheckBox, QMessageBox  # noqa: E402

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


# ---- verification additions (opt-out and remove-confirmation guards) ----------
def _update_threads():
    import threading

    return [t for t in threading.enumerate() if t.name == "nadare-update-check"]

def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _settle(qapp, seconds=1.0):
    """Give a (wrongly) started background removal time to finish."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def test_update_check_opt_out_never_starts_the_check(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck

    calls: list[int] = []
    monkeypatch.setattr(updatecheck, "check_latest", lambda *a, **k: calls.append(1))
    cfg = _empty_config(tmp_path)
    cfg.data["check_updates"] = False
    window = MainWindow(cfg)
    try:
        for t in _update_threads():
            t.join(2)
        qapp.processEvents()
        assert calls == []
    finally:
        _close(window, qapp)


def test_update_check_runs_when_enabled(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck

    calls: list[int] = []
    monkeypatch.setattr(updatecheck, "check_latest", lambda *a, **k: calls.append(1))
    window = MainWindow(_empty_config(tmp_path))
    try:
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert calls == [1]
    finally:
        _close(window, qapp)


def _window_with_trees(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install))
    cfg.save()
    return MainWindow(cfg), cfg, install


def test_cancel_in_remove_dialog_removes_nothing(tmp_path, qapp, monkeypatch):
    window, cfg, install = _window_with_trees(tmp_path, qapp, monkeypatch)
    try:
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)
        window._on_remove_requested("g")
        _settle(qapp)
        assert "g" in cfg.profiles
        assert (install / "game.exe").exists()
    finally:
        _close(window, qapp)


def test_second_confirmation_cancel_keeps_the_game_files(tmp_path, qapp, monkeypatch):
    window, cfg, install = _window_with_trees(tmp_path, qapp, monkeypatch)
    try:
        def fake_exec(self):
            for check in self.findChildren(QCheckBox):
                if "install folder" in check.text():
                    check.setChecked(True)
            return QMessageBox.StandardButton.Yes

        asked = []

        def fake_warning(*args, **kwargs):
            asked.append(args[2])
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        monkeypatch.setattr(QMessageBox, "warning", fake_warning)
        window._on_remove_requested("g")
        _settle(qapp)
        assert len(asked) == 1 and "permanently deletes" in asked[0]
        assert "g" in cfg.profiles
        assert (install / "game.exe").exists()
    finally:
        _close(window, qapp)


def test_unsafe_tree_checkbox_is_disabled_and_cancel_is_default(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(home))
    cfg.save()
    window = MainWindow(cfg)
    seen = {}
    try:
        def fake_exec(self):
            seen["checks"] = [(c.isEnabled(), c.isChecked()) for c in self.findChildren(QCheckBox)
                              if "install folder" in c.text()]
            seen["default"] = self.defaultButton().text() if self.defaultButton() else None
            seen["cancel"] = self.button(QMessageBox.StandardButton.Cancel).text()
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        window._on_remove_requested("g")
        assert seen["checks"] == [(False, False)]
        assert seen["default"] == seen["cancel"]
    finally:
        _close(window, qapp)


def test_game_view_shows_login_state(tmp_path, qapp):
    """A visible Logged in / Not logged in label, only for backends that report it."""
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    try:
        assert view.account_label.isHidden()
        view.set_status(Status("installed", data={"logged_in": True}))
        assert not view.account_label.isHidden()
        assert view.account_label.text() == "Logged in"
        view.set_status(Status("installed", data={"logged_in": False}))
        assert view.account_label.text() == "Not logged in"
        view.set_status(Status("not_installed", data={"logged_in": False}))
        assert view.account_label.isHidden()
        view.set_status(Status("installed", data={}))
        assert view.account_label.isHidden()
    finally:
        view.deleteLater()


def test_game_view_names_the_account_and_offers_login_first(tmp_path, qapp):
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "direct-mongil"})
    try:
        view.set_status(Status("installed", data={"logged_in": True, "account": "Cat"}))
        assert view.account_label.text() == "Logged in as Cat"
        view.set_status(Status("installed", data={"logged_in": False, "login_required": True}))
        assert view.account_label.text() == "Not logged in"
        assert view._primary_op() == "login"
        view.set_status(Status("installed", data={"logged_in": True, "login_required": False}))
        assert view._primary_op() == "launch"
    finally:
        view.deleteLater()


def test_failed_removal_clears_the_removing_status(tmp_path, qapp, monkeypatch):
    from nadare.core import removal

    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G")
    cfg.save()
    window = MainWindow(cfg)
    shown = []

    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(removal, "remove_game", boom)
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.critical", lambda *a, **k: shown.append(a[2])
    )
    try:
        window._remove_game("g", False)
        assert _wait_for(qapp, lambda: bool(shown))
        assert "Removing" not in window.side_status.text()
        assert "g" in cfg.profiles and "g" not in window._busy
    finally:
        window.close()


def test_enter_in_the_add_game_form_presses_no_button(tmp_path, qapp):
    """Not Browse (the old bug), and not Add game either: Enter only finishes typing.

    Add game used to be the default button, so Enter in the name field created the
    preselected game at once (tests/test_add_game.py has the key-press test).
    """
    from PyQt6.QtWidgets import QPushButton

    from nadare.gui.add_game import AddGameDialog

    dialog = AddGameDialog(_empty_config(tmp_path))
    try:
        buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
        assert not any(b.isDefault() for b in buttons.values())
        assert not buttons["Add game"].autoDefault()
        assert not buttons["Browse"].autoDefault()
        assert not buttons["Cancel"].autoDefault()
    finally:
        dialog.deleteLater()


def _notice_window(tmp_path, monkeypatch, kind, **profile):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend=kind, title="G", **profile)
    cfg.save()
    window = MainWindow(cfg)
    started = []
    monkeypatch.setattr(window, "_start", lambda p, op, *a, **k: started.append(op) or True)
    window._current_profile = "g"
    return window, cfg, started


def test_unofficial_notice_is_asked_once_per_game_type(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, cfg, started = _notice_window(tmp_path, monkeypatch, "stove-czn")
    answers = []
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: answers.append(self.informativeText()) or 1024
    )
    try:
        window._on_game_action("launch")
        window._on_game_action("launch")
        assert started == ["launch", "launch"]
        assert len(answers) == 1 and "publisher" in answers[0].lower()
        assert cfg.data["unofficial_notice_ack"] == ["stove-czn"]
        assert Config.load(tmp_path / "config.json").data["unofficial_notice_ack"] == ["stove-czn"]
    finally:
        window.close()


def test_declining_the_unofficial_notice_blocks_the_operation(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, cfg, started = _notice_window(tmp_path, monkeypatch, "stove-czn")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 4194304)  # Cancel
    try:
        window._on_game_action("install")
        assert started == [] and "unofficial_notice_ack" not in cfg.data
    finally:
        window.close()


def test_big_fresh_install_asks_for_confirmation_with_the_size(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, cfg, started = _notice_window(tmp_path, monkeypatch, "stellasora-yostar")
    cfg.data["unofficial_notice_ack"] = ["stellasora-yostar"]
    # The question also refuses when the game does not fit (#1316): give the fake disk room,
    # or the result depends on the host's /tmp size.
    from nadare.core import diskspace

    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1 << 50)
    asked = []
    answer = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or answer[0])
    try:
        window._on_game_action("install")
        assert started == [] and "21 GiB" in asked[0]
        answer[0] = QMessageBox.StandardButton.Yes
        window._on_game_action("install")
        assert started == ["install"]
    finally:
        window.close()


def test_small_or_unknown_installs_do_not_ask(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, cfg, started = _notice_window(tmp_path, monkeypatch, "wine")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("should not ask"))
    try:
        window._on_game_action("install")
        assert started == ["install"]
    finally:
        window.close()


def test_add_game_buttons_stay_visible_on_a_short_screen(tmp_path, qapp):
    from PyQt6.QtWidgets import QPushButton

    from nadare.gui.add_game import AddGameDialog

    dialog = AddGameDialog(_empty_config(tmp_path))
    try:
        dialog.resize(560, 380)  # far shorter than the form
        dialog.show()
        qapp.processEvents()
        for button in dialog.findChildren(QPushButton):
            if button.text() in ("Add game", "Cancel"):
                top_left = button.mapTo(dialog, button.rect().topLeft())
                assert dialog.rect().contains(button.rect().translated(top_left))
    finally:
        dialog.close()


def test_enter_presses_a_focused_main_window_button(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QMainWindow, QPushButton

    from nadare.gui import EnterActivatesButtons

    keyfilter = EnterActivatesButtons.install(qapp)
    window = QMainWindow()
    button = QPushButton("Go")
    window.setCentralWidget(button)
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    try:
        window.show()
        window.activateWindow()
        button.setFocus()
        qapp.processEvents()
        QTest.keyClick(button, Qt.Key.Key_Return)
        assert clicks == [1]
        QTest.keyClick(button, Qt.Key.Key_Space)  # Qt's own handling still works
        assert clicks == [1, 1]
    finally:
        qapp.removeEventFilter(keyfilter)
        window.close()


def test_tab_skips_scroll_frames_and_reaches_the_add_game_card(tmp_path, qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    window = MainWindow(_empty_config(tmp_path))
    try:
        window.show_library()  # with no games the window starts on the Store
        window.show()
        qapp.processEvents()
        window.activateWindow()
        window.library_button.setFocus()
        reached = []
        for _ in range(4):
            QTest.keyClick(window, Qt.Key.Key_Tab)
            qapp.processEvents()
            focus = qapp.focusWidget()
            reached.append(focus.objectName() if focus else "")
        # Library -> Downloads -> Settings -> the card: one more stop since the Downloads entry.
        assert "AddGameCard" in reached[:3]
        assert not any("Scroll" in name for name in reached)
    finally:
        window.close()


def test_close_warning_during_login_does_not_mention_game_files(tmp_path, qapp, monkeypatch):
    """#367: a QR login in flight is not a file write; the wording must say so."""
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw

    window = MainWindow(_empty_config(tmp_path))
    try:
        seen: list[str] = []
        monkeypatch.setattr(
            mw.QMessageBox,
            "warning",
            lambda *args, **kwargs: seen.append(args[2]) or QMessageBox.StandardButton.Close,
        )
        monkeypatch.setattr(window.controller, "launching_profiles", lambda: [])
        monkeypatch.setattr(window.controller, "mutating_profiles", lambda: ["czn"])
        monkeypatch.setattr(window.controller, "running_operation", lambda name: "login")
        assert window._confirm_close() is True
        assert "game files" not in seen[0]
        monkeypatch.setattr(window.controller, "running_operation", lambda name: "install")
        window._confirm_close()
        assert "game files" in seen[1]
    finally:
        window.close()
        window.deleteLater()


def test_progress_bar_shows_an_eta_once_it_has_enough_samples(qapp, monkeypatch):
    from nadare.gui import game_view
    from nadare.gui.game_view import GameView

    clock = iter(range(0, 1000))
    monkeypatch.setattr(game_view.time, "monotonic", lambda: float(next(clock)))
    view = GameView()
    view._set_progress(0.01)
    assert view.progress.format() == "%p%"
    for step in range(2, 30):
        view._set_progress(step / 100)
    assert "left" in view.progress.format()
    view._clear_busy_progress()
    assert view.progress.format() == "%p%"


def test_remove_busy_page_has_no_cancel_button_and_a_busy_bar(qapp):
    """#583: Remove runs outside the controller, so nothing can cancel it."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {})
    try:
        view.set_busy("remove")
        assert view.primary.isEnabled() is False
        assert view.primary.text() != "CANCEL"
        assert (view.progress.minimum(), view.progress.maximum()) == (0, 0)
        assert view.badge.text() == "UNINSTALLING"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_close_warns_while_a_game_is_being_removed(tmp_path, qapp, monkeypatch):
    """#583: closing mid-delete leaves files gone but the profile in config."""
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw

    window = MainWindow(_empty_config(tmp_path))
    try:
        seen: list[str] = []
        monkeypatch.setattr(
            mw.QMessageBox,
            "warning",
            lambda *args, **kwargs: seen.append(args[2]) or QMessageBox.StandardButton.Cancel,
        )
        monkeypatch.setattr(window.controller, "launching_profiles", lambda: [])
        window._busy["gone"] = "remove"
        assert window._confirm_close() is False
        assert "removed" in seen[0]
        window._busy.clear()
        assert window._confirm_close() is True
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("operation", ["install", "update", "repair"])
def test_close_warning_during_a_download_says_what_is_kept(tmp_path, qapp, monkeypatch, operation):
    """Closing stops the download and keeps the partial files; the warning must say so (and
    must not call a repair a sign-in)."""
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw

    window = MainWindow(_empty_config(tmp_path))
    try:
        seen: list[str] = []
        monkeypatch.setattr(
            mw.QMessageBox,
            "warning",
            lambda *args, **kwargs: seen.append(args[2]) or QMessageBox.StandardButton.Close,
        )
        monkeypatch.setattr(window.controller, "launching_profiles", lambda: [])
        monkeypatch.setattr(window.controller, "mutating_profiles", lambda: ["czn"])
        monkeypatch.setattr(window.controller, "running_operation", lambda name: operation)
        window._confirm_close()
        text = seen[0]
        assert "signing in" not in text
        assert "game files" in text
        assert "kept" in text and "carries on" in text
    finally:
        window.close()
        window.deleteLater()
