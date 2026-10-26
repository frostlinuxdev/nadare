# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the "Repair files" action (issue #195).

Repair reuses each backend's existing content comparison (CZN's manifest
hashes, Yostar's size+CRC plan) and downloads whatever differs, then reports.
Nothing here reaches the network: the backends' comparison and sync steps are
replaced with fakes.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.czn.backend import CznBackend
from nadare.core.backends import (
    BackendError,
    BackendUnsupported,
    GameBackend,
    Status,
)

# ---------------------------------------------------------------------------
# base contract
# ---------------------------------------------------------------------------


class _DummyBackend(GameBackend):
    def status(self) -> Status:
        return Status("not_installed")

    def install(self) -> None:
        pass

    def update(self) -> None:
        pass

    def launch(self) -> int:
        return 0

    def stop(self) -> None:
        pass


def test_the_default_repair_is_unsupported(tmp_path):
    backend = _DummyBackend(make_context(tmp_path, "script"))
    with pytest.raises(BackendUnsupported) as exc:
        backend.repair()
    assert "Repair" in exc.value.message


# ---------------------------------------------------------------------------
# CZN
# ---------------------------------------------------------------------------


def _czn_backend(tmp_path) -> CznBackend:
    return CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")}))


def test_czn_repair_updates_then_reports_a_clean_install(tmp_path, monkeypatch):
    backend = _czn_backend(tmp_path)
    events: list = []
    backend.ctx.emit = events.append
    calls: list[str] = []
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "update", lambda full=False: calls.append(("update", full)))
    monkeypatch.setattr(backend, "_differing_files", lambda: [])

    backend.repair()

    assert calls == [("update", True)]  # Repair must hash every file
    assert ("step", "Checking every game file") in [(e.kind, e.message) for e in events]
    assert ("done", "All game files check out") in [(e.kind, e.message) for e in events]


def test_czn_repair_fails_when_files_are_still_damaged(tmp_path, monkeypatch):
    backend = _czn_backend(tmp_path)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "update", lambda full=False: None)
    monkeypatch.setattr(backend, "_differing_files", lambda: ["/bin/a.dll", "/bin/b.dll"])

    with pytest.raises(BackendError) as exc:
        backend.repair()

    assert "2 game file(s) are still damaged" in exc.value.message
    assert "/bin/a.dll" in exc.value.message
    assert "reinstall" in exc.value.message


def test_czn_repair_refuses_while_the_game_is_running(tmp_path, monkeypatch):
    backend = _czn_backend(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(backend, "_game_running", lambda: True)
    monkeypatch.setattr(backend, "update", lambda: called.append("update"))

    with pytest.raises(BackendError) as exc:
        backend.repair()

    assert exc.value.code == 8
    assert called == []


# ---------------------------------------------------------------------------
# Arknights / Stella Sora
# ---------------------------------------------------------------------------


def _direct_arknights(tmp_path) -> tuple[ArknightsBackend, object]:
    install = tmp_path / "install"
    install.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    backend = ArknightsBackend(
        make_context(
            tmp_path,
            ak.KIND,
            {"manifest_file": str(manifest), "install_dir": str(install)},
        )
    )
    return backend, install


def test_direct_arknights_repair_syncs_then_verifies(tmp_path, monkeypatch):
    backend, _ = _direct_arknights(tmp_path)
    events: list = []
    backend.ctx.emit = events.append
    calls: list[str] = []
    monkeypatch.setattr(backend, "_sync_direct", lambda: calls.append("sync") or {})
    monkeypatch.setattr(backend, "_load_manifest", lambda: object())
    monkeypatch.setattr(ak.yostar, "plan", lambda manifest, directory, **kw: [])

    backend.repair()

    assert calls == ["sync"]
    assert ("done", "All game files check out") in [(e.kind, e.message) for e in events]


def test_direct_arknights_repair_reports_files_that_still_differ(tmp_path, monkeypatch):
    backend, _ = _direct_arknights(tmp_path)
    monkeypatch.setattr(backend, "_sync_direct", lambda: {})
    monkeypatch.setattr(backend, "_load_manifest", lambda: object())
    missing = [type("F", (), {"path": f"game/file{i}"})() for i in range(2)]
    monkeypatch.setattr(ak.yostar, "plan", lambda manifest, directory, **kw: missing)

    with pytest.raises(BackendError) as exc:
        backend.repair()

    assert "2 game file(s)" in exc.value.message
    assert "game/file0" in exc.value.message


def test_non_direct_arknights_repair_is_unsupported(tmp_path):
    backend = ArknightsBackend(make_context(tmp_path, ak.KIND))
    assert backend.direct_mode() is False
    with pytest.raises(BackendUnsupported) as exc:
        backend.repair()
    assert "only available when Nadare manages" in exc.value.message


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_config(tmp_path):
    from nadare.core.config import Config

    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("game", backend="wine", title="Game")
    config.save()
    return config, path


def test_the_repair_command_is_parsed_with_the_usual_profile_flags():
    from nadare import app

    args = app.build_parser().parse_args(["repair", "-p", "game", "--accept-account-notice"])
    assert args.command == "repair"
    assert args.profile == "game"
    assert args.accept_account_notice is True
    assert "repair" in app.COMMANDS


def test_the_repair_command_dispatches_to_the_backend(tmp_path, monkeypatch, capsys):
    from nadare import app

    config, path = _cli_config(tmp_path)
    calls: list[str] = []

    class Fake:
        def requires_notice_ack(self) -> bool:
            return False

        def repair(self) -> None:
            calls.append("repair")

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: Fake())

    assert app.run_cli(["-c", str(path), "repair", "-p", "game"]) == 0
    assert calls == ["repair"]


# ---------------------------------------------------------------------------
# GUI worker
# ---------------------------------------------------------------------------


def test_the_worker_dispatches_repair(tmp_path, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui import workers

    assert "repair" in workers.OPERATIONS
    assert "repair" in workers.MUTATING
    calls: list[str] = []

    class Fake:
        def repair(self) -> None:
            calls.append("repair")

    monkeypatch.setattr(workers, "create", lambda *a, **k: Fake())
    workers.BackendWorker(Config.load(tmp_path / "config.json"), "p", "repair").run()
    assert calls == ["repair"]


# ---------------------------------------------------------------------------
# GUI widgets
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_the_repair_button_needs_can_repair_and_an_idle_page(qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("g", {"backend": "stove-czn", "title": "G"})
    view.set_status(Status("installed", data={}))
    assert not view.repair_btn.isEnabled()

    view.set_status(Status("installed", data={"can_repair": True}))
    assert view.repair_btn.isEnabled()
    assert "missing or damaged" in view.repair_btn.toolTip()

    view.set_busy("repair")
    assert not view.repair_btn.isEnabled()
    view.deleteLater()


def test_repair_asks_first_and_does_nothing_on_no(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.core.config import Config
    from nadare.gui import main_window as mw

    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="stove-czn", title="Game")
    config.save()
    window = mw.MainWindow(config)
    started: list[str] = []
    monkeypatch.setattr(window, "_start", lambda p, op, *a, **k: started.append(op) or True)
    window._current_profile = "g"
    asked: list[str] = []
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2] if len(a) > 2 else "") or answers.pop(0),
    )
    try:
        window._on_game_action("repair")
        assert started == []
        assert asked and "Repair Game?" in asked[0]
        window._on_game_action("repair")
        assert started == ["repair"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
