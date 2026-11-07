# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed operation offers Retry and Open logs, and RETRY re-runs it (b21 polish)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _view(monkeypatch, choice):
    view = GameView()
    view.set_profile("p", {"title": "P", "backend": "wine"})
    asked: list[tuple[str, bool]] = []

    def pick(title, message, can_retry):
        asked.append((message, can_retry))
        return choice

    monkeypatch.setattr(view, "_failure_choice", pick)
    return view, asked


def test_a_failed_install_offers_retry_and_retry_runs_it_again(qapp, monkeypatch):
    view, asked = _view(monkeypatch, "retry")
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_busy("install")
    view.finish("install", 1)
    assert asked and asked[0][1] is True
    assert actions == ["install"]
    view.deleteLater()


def test_a_worker_failure_offers_retry_too(qapp, monkeypatch):
    view, asked = _view(monkeypatch, "retry")
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_busy("update")
    view.fail("disk is full")
    assert asked == [("disk is full", True)]
    assert actions == ["update"]
    view.deleteLater()


def test_open_logs_choice_opens_the_logs_and_close_does_nothing(qapp, monkeypatch):
    view, _asked = _view(monkeypatch, "logs")
    opened: list[bool] = []
    monkeypatch.setattr(view, "_open_logs", lambda: opened.append(True))
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_busy("install")
    view.finish("install", 1)
    assert opened == [True] and actions == []

    view2, _ = _view(monkeypatch, "close")
    actions2: list[str] = []
    view2.action_requested.connect(actions2.append)
    view2.set_busy("install")
    view2.finish("install", 1)
    assert actions2 == []
    view.deleteLater()
    view2.deleteLater()


def test_a_failure_that_cannot_be_retried_has_no_retry(qapp, monkeypatch):
    view, asked = _view(monkeypatch, "close")
    view.set_busy("remove")
    view.fail("could not delete")
    assert asked == [("could not delete", False)]
    view.deleteLater()


def test_the_retry_button_after_a_failure_reruns_the_failed_operation(qapp, monkeypatch):
    view, _ = _view(monkeypatch, "close")
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_busy("install")
    view.finish("install", 1)
    view.set_status(Status("error"))
    assert view.primary.text() == "RETRY"
    view.primary.click()
    assert actions == ["install"]
    view.deleteLater()


def test_the_retry_button_without_a_failed_operation_checks_the_status_again(qapp, monkeypatch):
    view, _ = _view(monkeypatch, "close")
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    view.set_status(Status("error"))
    view.primary.click()
    assert actions == ["status"]
    view.deleteLater()


def test_a_plain_error_still_uses_a_single_ok_box(qapp, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[1])))
    view = GameView()
    view.set_profile("p", {})
    view.show_error("Nadare could not finish", "x")
    assert warned == ["Nadare could not finish"]
    view.deleteLater()
