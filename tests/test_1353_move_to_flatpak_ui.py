# SPDX-License-Identifier: GPL-3.0-or-later
"""#1353: the Settings button, and the start-up that finishes an interrupted move."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from nadare.core import flatpakmove
from nadare.core.config import Config
from nadare.gui import settings


def _page(monkeypatch, installed: bool):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(flatpakmove, "probably_installed", lambda home=None: installed)
    return settings.SettingsPage()


def test_the_button_shows_only_with_the_flatpak_installed(monkeypatch):
    shown = _page(monkeypatch, True)
    hidden = _page(monkeypatch, False)
    assert not shown.move_to_flatpak_button.isHidden()
    assert hidden.move_to_flatpak_button.isHidden()


def test_a_refusal_is_shown_and_nothing_moves(monkeypatch, tmp_path):
    page = _page(monkeypatch, True)
    page._config = Config(path=tmp_path / "c.json", data={"profiles": {}})
    monkeypatch.setattr(flatpakmove, "refusal", lambda c: "The Nadare Flatpak is running.")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))
    monkeypatch.setattr(flatpakmove, "move", lambda c: pytest.fail("refused"))
    page.move_to_flatpak_button.click()
    assert warned == ["The Nadare Flatpak is running."]


def test_cancel_moves_nothing(monkeypatch, tmp_path):
    page = _page(monkeypatch, True)
    page._config = Config(path=tmp_path / "c.json", data={"profiles": {}})
    monkeypatch.setattr(flatpakmove, "refusal", lambda c: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    monkeypatch.setattr(flatpakmove, "move", lambda c: pytest.fail("cancelled"))
    page.move_to_flatpak_button.click()


def test_yes_moves_then_closes_nadare(monkeypatch, tmp_path):
    page = _page(monkeypatch, True)
    page._config = Config(path=tmp_path / "c.json", data={"profiles": {}})
    monkeypatch.setattr(flatpakmove, "refusal", lambda c: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    shown, moved, quit_ = [], [], []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(flatpakmove, "move", lambda c: moved.append(c) or ["Moved."])
    monkeypatch.setattr(QApplication, "quit", lambda: quit_.append(True))
    page.move_to_flatpak_button.click()
    assert moved and shown == ["Moved."] and quit_


def test_the_cli_start_finishes_an_interrupted_move(monkeypatch, capsys):
    from nadare import app

    calls = []
    monkeypatch.setattr(flatpakmove, "finish_interrupted",
                        lambda **k: calls.append(1) or ["Finished moving."])
    monkeypatch.setattr(flatpakmove, "moved_note", lambda **k: "Your games moved.")
    assert app.run_cli(["list"]) == 0
    err = capsys.readouterr().err
    assert calls and "Finished moving." in err and "Your games moved." in err


def test_the_cli_refuses_with_the_reason(monkeypatch, capsys):
    from nadare import app

    monkeypatch.setattr(flatpakmove, "refusal", lambda c: "The Nadare Flatpak is not installed.")
    assert app.run_cli(["move-to-flatpak", "--yes"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_the_cli_asks_for_yes_first(monkeypatch, capsys):
    from nadare import app

    monkeypatch.setattr(flatpakmove, "refusal", lambda c: None)
    monkeypatch.setattr(flatpakmove, "move", lambda c: pytest.fail("needs --yes"))
    assert app.run_cli(["move-to-flatpak"]) == 0
    assert "--yes" in capsys.readouterr().out


def test_the_cli_moves_with_yes(monkeypatch, capsys):
    from nadare import app

    monkeypatch.setattr(flatpakmove, "refusal", lambda c: None)
    monkeypatch.setattr(flatpakmove, "move", lambda c: ["Moved your games."])
    assert app.run_cli(["move-to-flatpak", "--yes"]) == 0
    assert "Moved your games." in capsys.readouterr().out
