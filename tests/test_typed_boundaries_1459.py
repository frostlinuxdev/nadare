# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared status and legacy verify boundary contracts (#1459)."""

import argparse
from types import SimpleNamespace

import pytest

from nadare import app
from nadare.core import gamefacts
from nadare.core.backends import BackendError, Status


@pytest.mark.parametrize(
    ("state", "data", "running", "action"),
    [
        ("installed", {}, False, "launch"),
        ("installed", {"incomplete": True}, False, "install"),
        ("not_installed", {"paused": True}, False, "install"),
        ("running", {}, False, "stop"),
        ("installed", {}, True, "stop"),
        ("error", {}, False, "retry"),
        ("not_installed", {"can_install": False}, False, "settings"),
        ("installed", {"update_available": True}, False, "update"),
        ("installed", {"login_required": True, "logged_in": False}, False, "login"),
    ],
)
def test_common_projection_contract(state, data, running, action):
    status = Status(state, running=running, data={**data, "backend_detail": ["opaque"]})
    facts = gamefacts.CommonStatus.from_status(status)
    assert facts.primary_operation() == action == gamefacts.primary_operation(status)
    assert facts.extensions == {"backend_detail": ["opaque"]}


@pytest.mark.parametrize("state", ["instaled", "", None, 4, ["installed"]])
def test_bad_state_is_logged_error(state, caplog):
    facts = gamefacts.CommonStatus.from_status(Status(state))
    assert facts.state == "error"
    assert facts.primary_operation() == "retry"
    assert "state" in caplog.text


@pytest.mark.parametrize("data", [None, [], "bad", {"incomplete": "false"}, {"logged_in": 1}])
def test_malformed_common_data_cannot_offer_launch(data, caplog):
    facts = gamefacts.CommonStatus.from_status(Status("installed", data=data))
    assert facts.state == "error"
    assert facts.primary_operation() == "retry"
    assert caplog.text


@pytest.mark.parametrize("extension", [None, False, {"strange": object()}, ["unknown"]])
def test_extensions_are_opaque(extension):
    facts = gamefacts.CommonStatus.from_status(Status("installed", data={"vendor": extension}))
    assert facts.extensions["vendor"] is extension
    assert facts.primary_operation() == "launch"


@pytest.mark.parametrize(
    ("report", "outcome"),
    [
        ({"verified": True}, "passed"),
        ({"verified": False}, "failed"),
        ({}, "failed"),
        ({"verified": "yes"}, "failed"),
        ({"outcome": "nothing_to_verify", "verified": False}, "nothing_to_verify"),
        ({"outcome": "not_installed"}, "not_installed"),
    ],
)
def test_legacy_verify_requires_explicit_success(report, outcome):
    from nadare.core.verification import VerifyResult

    assert VerifyResult.from_report(report).outcome == outcome


def test_cli_missing_verify_key_is_failure(monkeypatch):
    backend = SimpleNamespace(verify=lambda: {}, requires_notice_ack=lambda: False)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    events = []
    code = app._run_backend_command(
        argparse.Namespace(command="verify"), None, "box", None, events.append
    )
    assert code == 1
    assert "all checks passed" not in events[-1].message.lower()


def test_reworded_not_installed_still_guides_install(monkeypatch):
    from nadare.core.backends import BackendNotInstalled

    def fail(*a, **k):
        raise BackendNotInstalled("Please acquire game files first", code=7)

    monkeypatch.setattr("nadare.core.launchgate.play", fail)
    with pytest.raises(BackendError, match="nadare install -p box") as caught:
        app._launch_backend(object(), "box")
    assert caught.value.code == 7


@pytest.mark.parametrize("report", [{}, {"message": "all checks passed"}])
def test_gui_missing_verify_key_is_failure(qtbot, monkeypatch, tmp_path, report):
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow
    from nadare.gui.main_window_parts import operations

    warnings = []
    monkeypatch.setattr(operations.QMessageBox, "warning", lambda *a: warnings.append(a))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("box", backend="wine")
    window = MainWindow(config)
    qtbot.addWidget(window)
    window._on_result("box", "verify", report)
    assert warnings
    assert "all checks passed" not in warnings[-1][-1].lower()
    window.controller._workers.clear()
    window.close()


@pytest.mark.parametrize(
    "status",
    [
        Status("installed"),
        Status("installed", data={"incomplete": True}),
        Status("running"),
        Status("error"),
        Status("instaled"),
        Status("installed", data={"login_required": True, "logged_in": False}),
        Status("installed", data={"incomplete": "no"}),
    ],
)
def test_page_card_and_cli_share_common_decision(qtbot, monkeypatch, status):
    from nadare.gui.game_view import GameView
    from nadare.gui.library import GameCard

    backend = SimpleNamespace(status=lambda: status, requires_notice_ack=lambda: False)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    events = []
    app._run_backend_command(argparse.Namespace(command="status"), None, "box", None, events.append)
    emitted = events[-1].data
    cli_status = Status(
        emitted["state"],
        running=emitted["running"],
        data={k: v for k, v in emitted.items() if k not in ("state", "running", "version")},
    )
    page = GameView()
    card = GameCard("box", "Box", "wine")
    qtbot.addWidget(page)
    qtbot.addWidget(card)
    page.set_status(status)
    card.set_state(status)
    action = gamefacts.primary_operation(status)
    assert page._primary_op() == action == gamefacts.primary_operation(cli_status)
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QFocusEvent

    card.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    assert (not card.play_button.isHidden()) == (action == "launch")


def test_explicit_verify_outcome_is_validated():
    from nadare.core.verification import VerifyResult

    with pytest.raises(TypeError):
        VerifyResult()
    with pytest.raises(ValueError):
        VerifyResult("passsed")


@pytest.mark.parametrize("kind", ["script", "direct-bd2", "launcher-nikke"])
def test_representative_backends_project_without_interpreting_extensions(kind, tmp_path):
    from _backend_helpers import make_context

    from nadare.core.backends import create

    context = make_context(tmp_path, kind)
    backend = create(context.name, context.config)
    status = backend.status()
    facts = gamefacts.CommonStatus.from_status(status)
    assert facts.state == status.state
    # Extension values have no cross-view contract and cannot override common facts.
    status.data["vendor_payload"] = {"state": "running", "incomplete": "yes"}
    projected = gamefacts.CommonStatus.from_status(status)
    assert projected.primary_operation() == facts.primary_operation()
    status.data["incomplete"] = "yes"
    assert gamefacts.CommonStatus.from_status(status).state == "error"


@pytest.mark.parametrize(
    "report",
    [
        {"outcome": "passsed", "message": "all checks passed"},
        {"outcome": "passed", "verified": False, "message": "all checks passed"},
    ],
)
def test_invalid_or_contradictory_verify_never_claims_success(report):
    from nadare.core.verification import VerifyResult

    result = VerifyResult.from_report(report)
    assert result.outcome == "failed"
    assert "all checks passed" not in result.message.lower()


@pytest.mark.parametrize("command", ["launch", "verify"])
def test_reworded_category_drives_cli_recovery(monkeypatch, command):
    from nadare.core.backends import BackendNotInstalled

    def fail(*a, **k):
        raise BackendNotInstalled("Acquire the game files first", code=7)

    backend = SimpleNamespace(verify=fail, requires_notice_ack=lambda: False)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    monkeypatch.setattr("nadare.core.launchgate.play", fail)
    with pytest.raises(BackendError, match="nadare install -p box") as caught:
        app._run_backend_command(
            argparse.Namespace(command=command), None, "box", None, lambda e: None
        )
    assert caught.value.code == 7


@pytest.mark.parametrize(
    "status",
    [
        Status("installed", version=3),
        Status("installed", message=[]),
        Status("installed", running="false"),
        Status("installed", data={"latest_version": ["bad"]}),
    ],
)
def test_malformed_envelope_and_shared_text_fail_closed(status):
    facts = gamefacts.CommonStatus.from_status(status)
    assert facts.state == "error"
    assert facts.primary_operation() == "retry"
    assert not facts.install.installed


@pytest.mark.parametrize(
    ("report", "expected", "code"),
    [
        ({"verified": True}, "all checks passed", 0),
        ({"verified": False}, "nothing to verify", 1),
        ({"outcome": "nothing_to_verify", "message": "nothing to verify"}, "nothing to verify", 1),
        (
            {"outcome": "not_installed", "message": "Acquire files"},
            "Not installed. Run: nadare install -p box",
            1,
        ),
    ],
)
def test_cli_verify_outcomes_keep_messages_and_exit_codes(monkeypatch, report, expected, code):
    backend = SimpleNamespace(verify=lambda: report, requires_notice_ack=lambda: False)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    events = []
    assert (
        app._run_backend_command(
            argparse.Namespace(command="verify"), None, "box", None, events.append
        )
        == code
    )
    assert events[-1].message == expected


def test_english_text_without_category_does_not_trigger_install_guidance(monkeypatch):
    error = BackendError("A helper is not installed", code=4)

    def fail(*a, **k):
        raise error

    monkeypatch.setattr("nadare.core.launchgate.play", fail)
    with pytest.raises(BackendError) as caught:
        app._launch_backend(object(), "box")
    assert caught.value is error


def test_extension_keys_must_be_strings():
    facts = gamefacts.CommonStatus.from_status(Status("installed", data={1: "invalid key"}))
    assert facts.state == "error"
    assert all(isinstance(key, str) for key in facts.extensions)
