# SPDX-License-Identifier: GPL-3.0-or-later
"""b28 regressions for launch-failure recovery and the default runner label."""

import logging
from types import SimpleNamespace

import pytest

from nadare.core import backends, graphics, runners
from nadare.core.config import Config
from nadare.gui.main_window_parts.operations import OperationsMixin
from nadare.gui.settings_parts.extras import discover_runners


def test_haoplay_default_picker_names_the_runner_play_will_use(monkeypatch):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(runners, "all_runners", lambda: [])
    kind = "direct-gfl2-haoplay"
    family = runners.default_family(kind)
    assert family == "ge", "fixture must exercise Haoplay's new GE default"
    label, value = discover_runners(kind)[0]
    assert value == ""
    assert runners.pinned_tag(family) in label


@pytest.mark.parametrize("previous", [(True, "old stamp"), (False, None)])
def test_failed_launch_history_write_error_does_not_escape_gui_handler(
    tmp_path, monkeypatch, caplog, previous
):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("wuwa", backend="direct-wuwa", last_played="new provisional stamp")
    snapshot = {"launch_previous_played": previous}
    errors = []
    finished = []
    refreshed = []
    window = SimpleNamespace(
        config=cfg,
        _snapshot=lambda name: snapshot,
        library=SimpleNamespace(refresh=lambda profiles: refreshed.append(dict(profiles["wuwa"]))),
        _finish_launch_sheet=lambda name: finished.append(name),
        _mark_status_error=lambda name, message: errors.append(message),
        _current_profile="",
    )

    def disk_full():
        raise OSError("No space left on device")

    monkeypatch.setattr(cfg, "save", disk_full)
    caplog.set_level(logging.WARNING, logger="nadare.gui.main_window")
    try:
        OperationsMixin._failed_launch(window, "wuwa", "Game could not start")
    except OSError as exc:
        pytest.fail(f"save error escaped the GUI launch-failure handler: {exc}")
    assert errors, "the failed launch must still reach its error UI"
    assert errors == ["Game could not start"]
    assert finished == ["wuwa"]
    assert refreshed == [cfg.profiles["wuwa"]]
    assert ("last_played" in cfg.profiles["wuwa"]) == previous[0]
    if previous[0]:
        assert cfg.profiles["wuwa"]["last_played"] == previous[1]
    assert "launch_previous_played" not in snapshot
    assert "play history" in caplog.text.lower()
    assert "No space left on device" in caplog.text


@pytest.mark.parametrize("modern", [False, True])
def test_other_runner_labels_are_unchanged(monkeypatch, modern):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: modern)
    monkeypatch.setattr(runners, "all_runners", lambda: [])
    expected = f"Default · recommended ({runners.pinned_tag(runners.DEFAULT_FAMILY)})"
    for kind in {*backends.kinds(), *runners.PROTON_DEFAULT_BACKENDS, "", "script", "steam"}:
        if kind == "direct-gfl2-haoplay" and modern:
            continue
        if kind in runners.PROTON_DEFAULT_BACKENDS:
            assert discover_runners(kind) == [(expected, "")], kind
            assert discover_runners(kind, include_default=False) == [], kind
        else:
            assert discover_runners(kind) == [("System Wine", "")], kind


def test_default_runner_without_pin_keeps_the_fallback_label(monkeypatch):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(runners, "all_runners", lambda: [])
    monkeypatch.setattr(runners, "pinned_tag", lambda family: None)
    assert discover_runners("direct-gfl2-haoplay") == [
        ("Default · recommended (DW-Proton)", "")
    ]


@pytest.mark.parametrize("ending", ["event", "failed", "succeeded"])
def test_failed_launch_storage_error_stays_inside_qt_signal_delivery(
    tmp_path, qtbot, monkeypatch, ending
):
    from nadare.core.backends import Status
    from nadare.core.events import Event
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.data.update(check_updates=False, check_notices=False)
    config.add_profile("g", backend="wine", title="Example Game", last_played="old stamp")
    window = MainWindow(config)
    qtbot.addWidget(window)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    shown = []
    monkeypatch.setattr(
        window.game, "_failure_choice", lambda *args: shown.append(args) or "close"
    )
    window.show()
    window.open_game("g")
    window._on_result("g", "status", Status("installed"))
    window._start("g", "launch")
    window.controller.busy.emit("g", "launch", True)
    window.controller.backend_event.emit("g", Event("game_running"))
    sheet = window._launch_sheet
    assert sheet.isVisible()
    assert config.profiles["g"]["last_played"] != "old stamp"

    def disk_full():
        raise OSError("No space left on device")

    monkeypatch.setattr(config, "save", disk_full)
    with qtbot.captureExceptions() as exceptions:
        if ending == "event":
            window.controller.backend_event.emit("g", Event("error", "Game could not start"))
        elif ending == "failed":
            window.controller.failed.emit("g", "launch", "Game could not start")
        else:
            window.controller.succeeded.emit("g", "launch", 1)
    assert not exceptions, "a storage error escaped a Qt-delivered launch failure"
    assert not sheet.isVisible()
    assert window._launch_sheet is None
    assert window.centralWidget().isEnabled()
    assert window._snapshot("g")["status"].state == "error"
    assert config.profiles["g"]["last_played"] == "old stamp"
    if ending == "event":
        assert "Game could not start" in window._snapshot("g")["status"].message
    else:
        assert shown, "the launch-failure dialog must still be offered"
    window.hide()
