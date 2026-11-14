# SPDX-License-Identifier: GPL-3.0-or-later
"""Ubuntu's one-time permission: after Fix it, the game starts from the dialog (#1338 follow-up).

Since #1340 the AppArmor block is caught BEFORE the launch, so the dialog now comes up when the
player presses Play. Before: a successful fix said "Done. Press Install again." (wrong for Play)
and the player had to close the box and press Play again; Close never said that the game cannot
start without the permission. The user decided to keep the launch strict (no "play once without
umu"): Heroic and Lutris also stop here, without a fix in the app.
"""

from __future__ import annotations

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import userns  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.gui import userns_dialog  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402

MESSAGE = userns.with_hint(userns.BLOCKED_BEFORE_LAUNCH, blocked=True)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wait(qapp, predicate, seconds: float = 3.0) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end and not predicate():
        qapp.processEvents()
        time.sleep(0.01)
    assert predicate()


def _fixed(qapp, monkeypatch, retry_op, ok=True):
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    monkeypatch.setattr(userns, "install_profile", lambda: (ok, "Done." if ok else "Cancelled."))
    dialog = userns_dialog.UsernsDialog(MESSAGE, retry_op=retry_op)
    assert dialog.retry_button.isHidden()  # nothing to retry before the fix
    dialog.fix_button.click()
    _wait(qapp, lambda: dialog.fix_button.isEnabled() or dialog.fix_button.isHidden())
    return dialog


def test_the_dialog_says_the_game_cannot_start_without_it(qapp, monkeypatch):
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    dialog = userns_dialog.UsernsDialog(MESSAGE, retry_op="launch")
    text = dialog.summary.text()
    assert "cannot start until" in text
    assert "next time you press Play" in text
    assert "sudo" not in text


@pytest.mark.parametrize(
    ("retry_op", "label"),
    [("launch", "Play now"), ("install", "Install now"), ("update", "Update now"),
     ("repair", "Try again")],
)
def test_after_the_fix_one_button_runs_the_operation(qapp, monkeypatch, retry_op, label):
    dialog = _fixed(qapp, monkeypatch, retry_op)
    assert not dialog.retry_button.isHidden()
    assert dialog.retry_button.text() == label
    assert dialog.fix_button.isHidden()  # done: no second password prompt
    dialog.retry_button.click()
    assert dialog.retry_requested is True
    assert dialog.result() == userns_dialog.QDialog.DialogCode.Accepted


def test_a_failed_fix_offers_no_play_button(qapp, monkeypatch):
    dialog = _fixed(qapp, monkeypatch, "launch", ok=False)
    assert dialog.retry_button.isHidden()
    assert dialog.fix_button.isEnabled()
    assert dialog.retry_requested is False


def test_without_an_operation_the_status_says_what_to_press(qapp, monkeypatch):
    dialog = _fixed(qapp, monkeypatch, None)
    assert dialog.retry_button.isHidden()
    assert "Done." in dialog.status.text()
    assert "Install again" not in dialog.status.text()


def test_install_profile_success_text_names_no_one_button():
    """The dialog says what comes next; the core message must not claim "Press Install"."""
    import subprocess

    def ok(*a, **k):
        return subprocess.CompletedProcess(a, 0, "", "")

    _ok, message = userns.install_profile(run=ok)
    assert _ok is True
    assert "Install" not in message


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    window.open_game("czn")
    return window


def test_play_now_reruns_the_launch(qapp, tmp_path, monkeypatch):
    def exec_and_choose(self):
        self.retry_requested = True
        return 1

    monkeypatch.setattr(userns_dialog.UsernsDialog, "exec", exec_and_choose)
    window = _window(tmp_path)
    asked: list[str] = []
    window.game.action_requested.connect(asked.append)
    window.game.show_error("Nadare could not finish", MESSAGE, retry_op="launch", failed=True)
    assert asked == ["launch"]
    window.close()


def test_close_runs_nothing(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(userns_dialog.UsernsDialog, "exec", lambda self: 0)
    window = _window(tmp_path)
    asked: list[str] = []
    window.game.action_requested.connect(asked.append)
    window.game.show_error("Nadare could not finish", MESSAGE, retry_op="launch", failed=True)
    assert asked == []
    window.close()
