# SPDX-License-Identifier: GPL-3.0-or-later
"""One-click AppArmor fix: a friendly dialog with a pkexec "Fix it" button (#429)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import sandbox, userns

# ---- core ------------------------------------------------------------------------


def test_fix_script_writes_the_profile_and_loads_it(tmp_path: Path) -> None:
    target = tmp_path / "nadare-srt-bwrap"
    log = tmp_path / "parser.log"
    parser = tmp_path / "fake-parser"
    parser.write_text(f"#!/bin/sh\necho \"$@\" > {log}\n")
    parser.chmod(0o755)
    script = userns.fix_script(path=str(target), parser=str(parser))
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == userns.profile_text()
    assert log.read_text().strip() == f"-r {target}"


def test_fix_script_stops_if_the_profile_cannot_be_written(tmp_path: Path) -> None:
    parser = tmp_path / "fake-parser"
    parser.write_text(f"#!/bin/sh\ntouch {tmp_path}/ran\n")
    parser.chmod(0o755)
    script = userns.fix_script(path=str(tmp_path / "missing-dir" / "p"), parser=str(parser))
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / "ran").exists()  # never loads a profile it did not write


def test_default_script_targets_the_real_paths_only() -> None:
    script = userns.fix_script()
    assert userns.PROFILE_PATH in script
    assert "apparmor_parser -r" in script


def test_can_fix_needs_pkexec_and_no_flatpak(monkeypatch) -> None:
    monkeypatch.setattr(userns.shutil, "which", lambda name: "/usr/bin/pkexec")
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    assert userns.can_fix() is True
    monkeypatch.setattr(userns.shutil, "which", lambda name: None)
    assert userns.can_fix() is False
    monkeypatch.setattr(userns.shutil, "which", lambda name: "/usr/bin/pkexec")
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    assert userns.can_fix() is False


class _Done:
    def __init__(self, code: int, err: str = "") -> None:
        self.returncode = code
        self.stderr = err
        self.stdout = ""


def test_install_profile_runs_pkexec_with_the_fixed_script() -> None:
    seen: list[list[str]] = []

    def fake(argv, **kw):
        seen.append(argv)
        return _Done(0)

    ok, text = userns.install_profile(run=fake)
    assert ok is True
    assert seen == [["pkexec", "/bin/sh", "-c", userns.fix_script()]]
    assert "Install" in text  # tells the user to press Install again


@pytest.mark.parametrize(
    ("code", "word"),
    [(126, "cancel"), (127, "password")],
)
def test_install_profile_explains_a_dismissed_or_failed_prompt(code, word) -> None:
    ok, text = userns.install_profile(run=lambda argv, **kw: _Done(code))
    assert ok is False
    assert word in text.lower()


def test_install_profile_reports_a_failing_command() -> None:
    ok, text = userns.install_profile(run=lambda argv, **kw: _Done(1, "apparmor_parser: boom"))
    assert ok is False
    assert "boom" in text


def test_install_profile_survives_a_missing_pkexec() -> None:
    def fake(argv, **kw):
        raise FileNotFoundError("pkexec")

    ok, text = userns.install_profile(run=fake)
    assert ok is False
    assert "copy" in text.lower()


def test_hint_marker_splits_the_message() -> None:
    message = userns.with_hint("prefix creation failed (exit 1)", blocked=True)
    assert userns.has_hint(message)
    head, details = userns.split_hint(message)
    assert head == "prefix creation failed (exit 1)"
    assert userns.install_command() in details
    assert not userns.has_hint("prefix creation failed (exit 1)")


# ---- dialog ----------------------------------------------------------------------
try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config  # noqa: E402
from nadare.gui import userns_dialog  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


MESSAGE = userns.with_hint("prefix creation failed (exit 1)", blocked=True)


def _wait(qapp, predicate, seconds: float = 3.0) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end and not predicate():
        qapp.processEvents()
        time.sleep(0.01)
    assert predicate()


def test_dialog_is_friendly_and_hides_the_shell_text(qapp, monkeypatch) -> None:
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    dialog = userns_dialog.UsernsDialog(MESSAGE)
    shown = dialog.summary.text()
    assert "one-time permission" in shown
    assert "sudo" not in shown and "EOF" not in shown
    assert dialog.details.isHidden()
    assert userns.install_command() in dialog.details.toPlainText()
    dialog.details_button.click()
    assert not dialog.details.isHidden()
    dialog.details_button.click()
    assert dialog.details.isHidden()


def test_copy_button_copies_the_command(qapp, monkeypatch) -> None:
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    dialog = userns_dialog.UsernsDialog(MESSAGE)
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == userns.install_command()


def test_fix_button_is_hidden_without_pkexec(qapp, monkeypatch) -> None:
    monkeypatch.setattr(userns, "can_fix", lambda: False)
    dialog = userns_dialog.UsernsDialog(MESSAGE)
    assert dialog.fix_button.isHidden()
    assert not dialog.copy_button.isHidden()


def test_fix_button_runs_the_fix_and_reports_success(qapp, monkeypatch) -> None:
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    monkeypatch.setattr(userns, "install_profile", lambda: (True, "Done. Press Install again."))
    dialog = userns_dialog.UsernsDialog(MESSAGE)
    dialog.fix_button.click()
    assert not dialog.fix_button.isEnabled()  # no double-run while the prompt is open
    _wait(qapp, lambda: "Press Install again" in dialog.status.text())


def test_a_cancelled_prompt_lets_the_user_try_again(qapp, monkeypatch) -> None:
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    monkeypatch.setattr(userns, "install_profile", lambda: (False, "Cancelled."))
    dialog = userns_dialog.UsernsDialog(MESSAGE)
    dialog.fix_button.click()
    _wait(qapp, lambda: "Cancelled" in dialog.status.text())
    assert dialog.fix_button.isEnabled()


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    window.open_game("czn")
    return window


def test_backend_error_with_the_hint_opens_the_friendly_dialog(qapp, tmp_path, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        userns_dialog.UsernsDialog, "exec", lambda self: opened.append(self.summary.text()) or 0
    )
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[1])))
    window = _window(tmp_path)
    window.game.show_error("Backend error", MESSAGE)
    assert len(opened) == 1 and not warned
    window.game.show_error("Backend error", "something else broke")
    assert len(opened) == 1 and warned == ["Backend error"]
    window.close()
