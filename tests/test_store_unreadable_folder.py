# SPDX-License-Identifier: GPL-3.0-or-later
"""An install folder Nadare cannot read must be refused kindly, not blow up inside a Qt slot."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QFileDialog
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import add_game
from nadare.gui.quick_install import QuickInstallDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class _Unreadable:
    """A folder whose contents the user may not look at (EACCES, a stale mount)."""

    def __truediv__(self, _name):
        def boom():
            raise PermissionError(13, "Permission denied")

        return SimpleNamespace(exists=boom)


def test_a_folder_that_cannot_be_read_holds_no_game():
    game = SimpleNamespace(exes=["Game.exe"])
    assert add_game._holds_game(game, _Unreadable()) is False


def test_a_readable_folder_is_still_checked(tmp_path):
    game = SimpleNamespace(exes=["Game.exe"])
    assert add_game._holds_game(game, tmp_path) is False
    (tmp_path / "Game.exe").write_bytes(b"MZ")
    assert add_game._holds_game(game, tmp_path) is True


def _dialog(qapp, monkeypatch, *, picks, bad=("/bad",)):
    seen: list[str | None] = []

    def describe(chosen):
        seen.append(chosen)
        if chosen in bad:
            raise PermissionError(13, "Permission denied", chosen)
        return (chosen or "/default", "10 GB free", "")

    dialog = QuickInstallDialog("Game", notice="", describe=describe)
    picked = iter(picks)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: next(picked))
    return dialog, seen


def test_an_unreadable_pick_keeps_the_old_folder_and_says_why(qapp, monkeypatch):
    dialog, seen = _dialog(qapp, monkeypatch, picks=["/bad"])
    before = dialog.folder_text
    dialog.change_button.click()  # must not raise
    assert dialog.folder_text == before
    assert dialog.chosen_folder == ""
    text = dialog.summary_label.text()
    assert "/bad" in text and "another folder" in text
    assert dialog.summary_label.isVisibleTo(dialog)
    assert seen[-1] is None, "the page's state was restored to the folder still in use"


def test_a_good_pick_after_a_bad_one_works_and_clears_the_message(qapp, monkeypatch):
    dialog, _seen = _dialog(qapp, monkeypatch, picks=["/bad", "/good"])
    dialog.change_button.click()
    dialog.change_button.click()
    assert dialog.chosen_folder == "/good"
    assert "/good" in dialog.folder_label.text()
    assert "another folder" not in dialog.summary_label.text()


def test_a_normal_pick_is_unchanged(qapp, monkeypatch):
    dialog, _seen = _dialog(qapp, monkeypatch, picks=["/fine"])
    dialog.change_button.click()
    assert dialog.chosen_folder == "/fine"
    assert "/fine" in dialog.folder_label.text()


def test_cancelling_the_picker_changes_nothing(qapp, monkeypatch):
    dialog, seen = _dialog(qapp, monkeypatch, picks=[""])
    calls = len(seen)
    dialog.change_button.click()
    assert dialog.chosen_folder == "" and len(seen) == calls
