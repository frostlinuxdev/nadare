# SPDX-License-Identifier: GPL-3.0-or-later
"""Operation presentation contracts, without constructing MainWindow."""
from dataclasses import FrozenInstanceError

import pytest

from nadare.core.events import Event
from nadare.gui.operation_presentation import REPLAY_EVENTS, OperationPresentation


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_lifecycle(outcome):
    model = OperationPresentation()
    model.busy_changed("alpha", "install", True)
    assert model.view("alpha").busy == "install"
    model.queued("alpha", "install")
    assert model.view("alpha").queued
    model.busy_changed("alpha", "install", False)
    model.paused("alpha", "install")
    assert model.view("alpha").paused == "install"
    assert model.view("alpha").busy is None
    token = model.busy_changed("alpha", "install", True)
    assert not model.view("alpha").queued
    assert model.view("alpha").paused is None
    assert model.complete("alpha", "install", outcome, message="disk full", token=token)
    view = model.view("alpha")
    assert view.busy is None
    assert view.outcome == outcome
    assert view.failure == ("disk full" if outcome == "failed" else None)


def test_wrong_profile_completion_cannot_clear_either_run():
    model = OperationPresentation()
    alpha = model.busy_changed("alpha", "install", True)
    model.busy_changed("beta", "install", True)
    before = model.view("beta")
    assert not model.complete("beta", "install", "succeeded", token=alpha)
    assert model.view("beta") == before
    assert model.view("alpha").busy == "install"


def test_stale_completion_from_older_worker():
    model = OperationPresentation()
    old = model.busy_changed("alpha", "install", True)
    current = model.busy_changed("alpha", "install", True)
    assert not model.complete("alpha", "install", "failed", message="old", token=old)
    assert model.view("alpha").busy == "install"
    assert model.view("alpha").failure is None
    assert model.complete("alpha", "install", "succeeded", token=current)


def test_switch_view_and_late_background_completion_preserve_run():
    model = OperationPresentation()
    model.busy_changed("alpha", "install", True)
    model.event("alpha", Event("progress", "half", .5))
    model.busy_changed("beta", "launch", True)
    alpha = model.view("alpha")
    assert model.view("beta").events == ()
    assert model.view("alpha") == alpha
    model.busy_changed("alpha", "status", False)
    model.complete("alpha", "status", "succeeded")
    assert model.view("alpha") == alpha
    assert alpha.events[-1].progress == .5


def test_replay_is_bounded_immutable_and_resets_only_on_new_run():
    model = OperationPresentation()
    model.busy_changed("alpha", "install", True)
    event = Event("step", "original", data={"nested": [1, {"n": 2}]})
    model.event("alpha", event)
    snapshot = model.view("alpha")
    event.message = "changed"
    event.data["nested"].append(3)
    assert snapshot.events[0].message == "original"
    assert len(snapshot.events[0].data["nested"]) == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.busy = "launch"
    with pytest.raises(TypeError):
        snapshot.events[0].data["nested"][1]["n"] = 3
    for i in range(REPLAY_EVENTS + 7):
        model.event("alpha", Event("log", str(i)))
    replay = model.view("alpha").events
    assert len(replay) == REPLAY_EVENTS
    assert replay[0].message == "7"
    model.busy_changed("alpha", "install", False)
    assert model.view("alpha").events == replay
    model.busy_changed("alpha", "install", True)
    assert model.view("alpha").events == ()
    model.event("beta", Event("log", "not busy"))
    assert model.view("beta").events == ()


@pytest.mark.parametrize("operation", ["status", "news", "media", "check_update"])
def test_background_never_becomes_busy(operation):
    model = OperationPresentation()
    model.busy_changed("alpha", operation, True)
    assert model.view("alpha").busy is None


def test_scheduler_can_clear_paused_state_without_ending_another_run():
    model = OperationPresentation()
    model.paused("alpha", "install")
    model.sync_paused("alpha", "")
    assert model.view("alpha").paused is None
    model.busy_changed("alpha", "launch", True)
    model.sync_paused("alpha", "")
    assert model.view("alpha").busy == "launch"
