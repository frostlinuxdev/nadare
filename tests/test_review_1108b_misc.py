# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the 2026-11-08 review findings fixed together (misc wave)."""

from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare import app as cli_app
from nadare.core import directdl as dd
from nadare.core import gamesandbox, linkrelay, paths, removal, runlog, sophon
from nadare.core.config import Config
from nadare.core.sandboxed import SandboxMixin

# ---------------------------------------------------------------- GUI (cancel/remove/notice)
try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    QApplication = None


@pytest.fixture(scope="module")
def qapp():
    if QApplication is None:
        pytest.skip("PyQt6 is not importable in this interpreter")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    from nadare.gui.main_window import MainWindow
    from nadare.gui.workers import BackendController

    monkeypatch.setattr(BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("wuwa", backend="direct-wuwa", title="Wuthering Waves")
    config.save()
    win = MainWindow(config)
    win.open_game("wuwa")
    yield win
    win._busy.clear()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_a_paused_install_wins_over_a_running_background_probe(window, monkeypatch):
    asked = []
    answer = {"choice": "keep"}
    cancelled = []
    monkeypatch.setattr(window.controller, "running_operation", lambda p: "status")
    monkeypatch.setattr(window.controller, "paused_operation", lambda p: "install")
    monkeypatch.setattr(window.controller, "waiting_operation", lambda p: "")
    monkeypatch.setattr(
        window, "_ask_cancel_choice",
        lambda title, first_install: asked.append((title, first_install)) or answer["choice"],
    )
    monkeypatch.setattr(window.controller, "cancel", cancelled.append)
    window._cancel_with_choice("wuwa")
    assert asked == [("Wuthering Waves", True)]
    assert cancelled == ["wuwa"]


def test_a_waiting_install_gets_the_same_question(window, monkeypatch):
    asked = []
    cancelled = []
    monkeypatch.setattr(window.controller, "running_operation", lambda p: "status")
    monkeypatch.setattr(window.controller, "paused_operation", lambda p: "")
    monkeypatch.setattr(window.controller, "waiting_operation", lambda p: "install")
    monkeypatch.setattr(
        window, "_ask_cancel_choice",
        lambda title, first_install: asked.append((title, first_install)) or "keep",
    )
    monkeypatch.setattr(window.controller, "cancel", cancelled.append)
    window._cancel_with_choice("wuwa")
    assert asked == [("Wuthering Waves", True)]
    assert cancelled == ["wuwa"]


def test_controller_cancel_stops_both_a_background_worker_and_a_paused_install(tmp_path):
    from nadare.gui.workers import BackendController

    class Worker:
        operation = "status"

        def __init__(self):
            self.cancelled = False
            self._running = True

        def isRunning(self):  # noqa: N802 - Qt naming
            return self._running

        def cancel(self):
            self.cancelled = True

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    controller = BackendController(config)
    worker = Worker()
    controller._workers["p"] = worker
    controller._paused["p"] = ("install", None)
    cancelled = []
    controller.cancelled.connect(lambda _p, op: cancelled.append(op))
    controller.cancel("p")
    assert worker.cancelled is True
    assert "p" not in controller._paused
    assert cancelled == ["install"]


def test_stop_and_remove_keeps_the_page_busy_with_the_removal(window, monkeypatch):
    busy = []
    cancelled = []
    started = []
    window._remove_after_cancel.add("wuwa")

    def remove(profile, preselect=frozenset()):
        started.append((profile, preselect))
        window._busy[profile] = "remove"

    monkeypatch.setattr(window, "_on_remove_requested", remove)
    monkeypatch.setattr(window.game, "set_busy", busy.append)
    monkeypatch.setattr(window.game, "cancelled", lambda: cancelled.append(True))
    window._on_cancelled("wuwa", "install")
    assert started == [("wuwa", frozenset({"install_dir", "prefix"}))]
    assert busy == ["remove"]
    assert cancelled == []
    assert window._busy["wuwa"] == "remove"


def test_a_snapshot_installed_status_folds_the_page_notice(qapp):
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameSnapshot, GameView

    view = GameView()
    try:
        view.set_profile(
            "gfl2",
            {"backend": "direct-gfl2", "title": "GFL2"},
            snapshot=GameSnapshot(status=Status("installed")),
        )
        assert "How this game runs" in view.notice_label.text()
        assert "Show less" not in view.notice_label.text()
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------- core: directdl slot record
def test_a_non_string_done_slot_does_not_abort_the_update(tmp_path):
    plan = dd.Plan(
        "T",
        "2",
        archives=(
            dd.Archive(
                "new.zip",
                dd.RemoteFile("new.zip", "https://example.invalid/new.zip"),
                slot="client",
            ),
        ),
    )
    state = tmp_path / dd.STATE_DIR
    state.mkdir()
    (state / "old.done").write_text(json.dumps({"slot": []}), encoding="utf-8")
    assert dd._superseded_records(plan, tmp_path) == []


# ---------------------------------------------------------------- core: doctor partial output
def test_doctor_prints_already_collected_diagnostics_when_a_later_probe_fails(
    monkeypatch, capsys
):
    def failing_report(config, args, blockers):
        print("GPU/Vulkan: fake but useful")
        raise RuntimeError("vulkaninfo exploded")

    monkeypatch.setattr(cli_app, "_doctor_report", failing_report)
    with pytest.raises(RuntimeError):
        cli_app._run_doctor(object())
    assert "GPU/Vulkan: fake but useful" in capsys.readouterr().out

    text = cli_app._doctor_text(object())
    assert "GPU/Vulkan: fake but useful" in text
    assert "unavailable: vulkaninfo exploded" in text


# ---------------------------------------------------------------- core: legacy log folders
def test_a_generated_looking_key_keeps_its_existing_legacy_log_folder(tmp_path):
    name = "game-000000"
    legacy = paths.logs_dir() / name
    legacy.mkdir(parents=True)
    (legacy / "old.log").write_text("old", encoding="utf-8")
    assert runlog.game_id(name) == name

    # Once the hashed folder exists too, new runs use it; removal cleans both.
    hashed = paths.logs_dir() / f"{name}-{hashlib.sha256(name.encode()).hexdigest()[:6]}"
    hashed.mkdir()
    assert runlog.game_id(name) == hashed.name


def test_removal_deletes_both_log_folders_when_both_exist(tmp_path, monkeypatch):
    name = "game-000000"
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(name, backend="wine", title="Game")
    cfg.save()

    class Fake:
        def status(self):
            from nadare.core.backends import Status

            return Status("installed")

    monkeypatch.setattr(removal.backends, "create", lambda *a, **k: Fake())
    hashed_name = f"{name}-{hashlib.sha256(name.encode()).hexdigest()[:6]}"
    legacy = paths.logs_dir() / name
    hashed = paths.logs_dir() / hashed_name
    legacy.mkdir(parents=True)
    hashed.mkdir()
    (legacy / "old.log").write_text("old", encoding="utf-8")
    (hashed / "new.log").write_text("new", encoding="utf-8")

    removal.remove_game(cfg, name, delete_data=True, applications_dir=tmp_path / "apps")
    assert not legacy.exists()
    assert not hashed.exists()


# ---------------------------------------------------------------- core: link relay cleanup
def test_a_failed_relay_start_leaves_no_temp_folder_behind(tmp_path, monkeypatch):
    relay = linkrelay.LinkRelay(tmp_path)

    def fail_fifo(*args, **kwargs):
        raise OSError("no fifo")

    monkeypatch.setattr(linkrelay.os, "mkfifo", fail_fifo)
    with pytest.raises(OSError):
        relay.start()
    assert [path for path in tmp_path.iterdir() if path.name.startswith("nadare-links-")] == []


class _Mixin(SandboxMixin):
    def __init__(self, tmp_path):
        self.ctx = SimpleNamespace(profile={}, emit=lambda event: None)
        self._link_relay = None
        self.stopped = []
        self.tmp_path = tmp_path

    def secure_mode_enabled(self):
        return True

    def _sandbox_check(self, subject):
        return gamesandbox.Located(binary="/usr/bin/bwrap")

    def _sandbox_prefix(self):
        return self.tmp_path

    def _sandbox_game_dir(self):
        return None

    def info(self, *args, **kwargs):
        pass

    def _stop_link_relay(self):
        relay, self._link_relay = self._link_relay, None
        self.stopped.append(relay)
        if relay is not None:
            relay.stop()


def test_secure_wrap_stops_the_relay_when_wrapping_raises_unexpectedly(tmp_path, monkeypatch):
    backend = _Mixin(tmp_path)
    monkeypatch.setattr(linkrelay.LinkRelay, "start", lambda self: tmp_path / "pipe")
    monkeypatch.setattr(linkrelay.LinkRelay, "stop", lambda self: setattr(self, "stopped", True))
    monkeypatch.setattr(
        gamesandbox, "wrap", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("path loop"))
    )
    with pytest.raises(RuntimeError):
        backend._secure_wrap(["wine"], {})
    assert backend._link_relay is None
    assert any(getattr(relay, "stopped", False) for relay in backend.stopped if relay)


def test_the_wine_launch_stops_the_relay_if_it_fails_before_streaming(tmp_path, monkeypatch):
    from nadare.backends import wine

    backend = object.__new__(wine.WineBackend)
    backend.ctx = SimpleNamespace(
        cancel=SimpleNamespace(raise_if_cancelled=lambda: None),
        profile={},
        emit=lambda event: None,
    )
    relay = SimpleNamespace(stopped=False)

    def fake_build():
        backend._link_relay = relay
        return ["wine", "game.exe"], {}, tmp_path, tmp_path / "game.exe"

    def fail_info(*args, **kwargs):
        raise RuntimeError("could not print the command")

    backend.build = fake_build
    backend._runner = lambda: None
    backend.info = fail_info
    backend._run_vcredist = lambda: None
    backend._stop_link_relay = lambda: (
        setattr(relay, "stopped", True),
        setattr(backend, "_link_relay", None),
    )
    with pytest.raises(RuntimeError):
        backend.launch()
    assert relay.stopped is True


def test_the_direct_launch_stops_the_relay_if_it_fails_before_streaming(tmp_path, monkeypatch):
    from nadare.backends import winegame

    exe = tmp_path / "game.exe"
    exe.write_text("MZ", encoding="utf-8")
    backend = object.__new__(winegame.WineGameBackend)
    backend.ctx = SimpleNamespace(cancel=SimpleNamespace(), profile={}, emit=lambda event: None)
    relay = SimpleNamespace(stopped=False)
    backend.direct_target = lambda: exe
    backend._run_vcredist = lambda created=False: None
    backend._prefix_command = lambda argv, env=None, **_kw: (argv, {})
    backend._direct_argv = lambda target: [str(target)]
    backend.launch_extra_env = lambda: None

    def fake_wrap(argv, env):
        backend._link_relay = relay
        return argv, env

    def fail_info(*args, **kwargs):
        raise RuntimeError("could not print the command")

    backend._secure_wrap = fake_wrap
    backend.info = fail_info
    backend._stop_link_relay = lambda: (
        setattr(relay, "stopped", True),
        setattr(backend, "_link_relay", None),
    )
    with pytest.raises(RuntimeError):
        backend._launch_direct()
    assert relay.stopped is True


# ---------------------------------------------------------------- core: sophon cleanup
def test_sophon_cleanup_is_incomplete_when_a_stale_path_has_a_symlinked_parent(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    ledger = tmp_path / sophon.STATE_DIR / "sophon-T.files"
    assert sophon._cleanup_stale_files(tmp_path, ["linked/kept.bin"], [], ledger) is False
