# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-2 audit: GUI/CLI truthfulness regressions."""

from __future__ import annotations

import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare import app as nadare_app
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="arknights-yostar", title="A")
    config.add_profile("b", backend="arknights-yostar", title="B")
    return MainWindow(config)


def test_running_launch_is_truthful(qapp):
    view = GameView()
    view.set_status(Status("installed"))
    view.set_busy("launch")
    assert not view.progress.isVisible()
    assert view.badge.text() == "RUNNING"
    assert view.primary.text() == "STOP"


def test_cancellable_op_keeps_cancel(qapp):
    view = GameView()
    view.set_busy("install")
    assert view.primary.text() == "CANCEL"


def test_error_state_disables_launch_and_verify(qapp):
    view = GameView()
    view.set_status(Status("error", message="boom"))
    assert not view.primary.isEnabled()
    assert not view.verify.isEnabled()


def test_not_installable_wine_shows_settings_hint(qapp):
    view = GameView()
    view.set_status(Status("not_installed", data={"can_install": False}))
    assert "SETTINGS" in view.primary.text().upper()
    assert not view.primary.isEnabled()


def test_wine_missing_exe_reports_cannot_install(tmp_path):
    from nadare.backends.wine import WineBackend  # noqa: F401
    from nadare.core.backends import create as create_backend

    config = Config.load(tmp_path / "config.json")
    config.add_profile("w", backend="wine", title="W")
    status = create_backend("w", config).status()
    assert status.state == "not_installed"
    assert status.data.get("can_install") is False


def test_library_keeps_badges_after_refresh(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        for name in ("a", "b"):
            window._on_result(name, Status("installed"))
        window.refresh_library()
        for name in ("a", "b"):
            assert window.library._cards[name].badge.text() == "READY"
    finally:
        window.close()


def test_verify_dialog_message(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    seen = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda p, t, m: seen.append(("info", m)))
    )
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda p, t, m: seen.append(("warn", m)))
    )
    try:
        window._on_result("a", {"verified": True, "exe": "/g/game.exe"})
        window._on_result("a", {"verified": False})
        assert seen[0] == ("info", "All checks passed")
        assert seen[1][0] == "warn" and "{" not in seen[1][1]
    finally:
        window.close()


class _FakeBackend:
    def __init__(self, before, after=None):
        self._statuses = [before] + ([after] if after else [before])
        self.stopped = False

    def status(self):
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def stop(self):
        self.stopped = True


def _cli(monkeypatch, backend, command):
    monkeypatch.setattr(nadare_app, "create_backend", lambda *a, **k: backend)
    args = types.SimpleNamespace(command=command)
    return nadare_app._run_backend_command(args, None, "arknights", None, lambda e: None)


def test_cli_status_prints_state_and_exit(monkeypatch, capsys):
    rc = _cli(monkeypatch, _FakeBackend(Status("installed", version="041.2.0")), "status")
    assert rc == 0
    assert "arknights: installed 041.2.0" in capsys.readouterr().out
    rc = _cli(monkeypatch, _FakeBackend(Status("error", message="bad")), "status")
    assert rc == 1
    rc = _cli(monkeypatch, _FakeBackend(Status("not_installed")), "status")
    assert rc == 0
    assert "not installed" in capsys.readouterr().out


def test_cli_stop_reports(monkeypatch, capsys):
    rc = _cli(monkeypatch, _FakeBackend(Status("installed")), "stop")
    assert rc == 1
    assert "not running" in capsys.readouterr().out
    rc = _cli(
        monkeypatch,
        _FakeBackend(Status("running", running=True), Status("installed")),
        "stop",
    )
    assert rc == 0
    assert "stopped" in capsys.readouterr().out


def _settings(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("s", backend="wine", title="Old")
    page = SettingsPage()
    page.load(config, "s")
    return config, page


def test_failed_save_rolls_back(tmp_path, qapp, monkeypatch):
    config, page = _settings(tmp_path)
    shown = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda p, t, m: shown.append(m))
    )

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Config, "save", boom)
    page.title.setText("New")
    page.save()
    assert config.profile("s")["title"] == "Old"
    assert shown and "disk full" in shown[0]


def test_fetch_art_does_not_mutate_until_save(tmp_path, qapp):
    config, page = _settings(tmp_path)
    art = tmp_path / "cover.png"
    art.write_bytes(b"x")
    page._on_art_fetched("s", {"cover": str(art)})
    assert "art_source" not in config.profile("s")
    page.load(config, "s")  # Cancel / reopen discards
    page.save()
    assert "art_source" not in config.profile("s")
    page._on_art_fetched("s", {"cover": str(art)})
    page.save()
    assert "art_source" in config.profile("s")
