# SPDX-License-Identifier: GPL-3.0-or-later
"""W6-5 (#1310): Repair syncs to the published version, so its question must say so.

Repair fetches the publisher's current file list; damaged files are replaced with the
current version's files and the version record follows. That is correct (the old
version is not downloadable), but the question used to promise only damaged files.
"""
from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QMessageBox

from nadare.gui.main_window_parts import confirm


def test_the_repair_question_says_an_available_update_is_installed_too(monkeypatch):
    asked: list[str] = []

    def question(parent, title, text, *args):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    monkeypatch.setattr(confirm.QMessageBox, "question", staticmethod(question))
    window = SimpleNamespace(_game_title=lambda profile: "Brown Dust 2")
    assert confirm.ConfirmMixin._confirm_repair(window, "bd2") is False
    text = asked[0]
    assert "update" in text.lower() and "installs it too" in text, text
