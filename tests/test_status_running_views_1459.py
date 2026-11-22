# SPDX-License-Identifier: GPL-3.0-or-later
"""A malformed running status is an error in every view (#1459, simulation BUG 1).

``Status("running", running=1)`` fails validation, and ``running=1`` is not a running
signal: the page must offer RETRY, like the card and the CLI. A validation failure that
still carries the explicit boolean ``running=True`` keeps Stop, because the game may
really be running; the card and the CLI carry the same fact.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from nadare import app
from nadare.core import gamefacts
from nadare.core.backends import Status


@pytest.mark.parametrize(
    "status",
    [
        Status("running", running=1),
        Status("installed", running="yes"),
        Status("running", running=True, data=[]),
    ],
)
def test_a_status_that_fails_validation_is_an_error(status):
    facts = gamefacts.CommonStatus.from_status(status)
    assert facts.state == "error"
    assert not facts.install.installed


def test_running_one_is_not_a_running_signal():
    facts = gamefacts.CommonStatus.from_status(Status("running", running=1))
    assert facts.running is False
    assert facts.primary_operation() == "retry"


def test_an_explicit_running_true_keeps_stop_after_validation_fails():
    facts = gamefacts.CommonStatus.from_status(Status("running", running=True, data=[]))
    assert facts.state == "error"
    assert facts.running is True
    assert facts.primary_operation() == "stop"


@pytest.mark.parametrize(
    ("status", "expected_running", "expected_operation", "expected_primary"),
    [
        (Status("running", running=1), False, "retry", "RETRY"),
        (Status("running", running=True, data=[]), True, "stop", "PLAYING"),
    ],
)
def test_page_card_and_cli_agree_on_a_malformed_running_status(
    qtbot, monkeypatch, status, expected_running, expected_operation, expected_primary
):
    from nadare.gui.game_view import GameView
    from nadare.gui.library import GameCard

    backend = SimpleNamespace(status=lambda: status, requires_notice_ack=lambda: False)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    events: list = []
    code = app._run_backend_command(
        argparse.Namespace(command="status"), None, "box", None, events.append
    )
    # A status that fails validation is an error even when a game is really running.
    assert code == 1
    assert events[-1].kind == "error"
    assert events[-1].data["running"] is expected_running

    page = GameView()
    card = GameCard("box", "Box", "wine")
    qtbot.addWidget(page)
    qtbot.addWidget(card)
    page.set_status(status)
    card.set_state(status)

    assert gamefacts.primary_operation(status) == expected_operation
    assert page._primary_op() == expected_operation
    assert page.primary.text() == expected_primary
    assert page.stop_button.isHidden() is (expected_operation != "stop")
    assert card.property("running") == expected_running
    assert card.play_button.isHidden()
    assert card.badge.text() == "ERROR"
