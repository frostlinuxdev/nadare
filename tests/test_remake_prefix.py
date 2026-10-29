# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for rebuilding a corrupted Wine prefix (#8).

The heavy steps (wineboot, WebView2, VC++, game download) are replaced with
fakes; the file-move helpers run for real on ``tmp_path``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.czn.backend import CznBackend
from nadare.core import prefixmove
from nadare.core.backends import BackendError, BackendUnsupported, GameBackend, Status


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


def test_the_default_remake_prefix_is_unsupported(tmp_path):
    backend = _DummyBackend(make_context(tmp_path, "script"))
    with pytest.raises(BackendUnsupported) as exc:
        backend.remake_prefix()
    assert "Wine prefix" in exc.value.message


# ---------------------------------------------------------------------------
# CZN
# ---------------------------------------------------------------------------


def _czn(tmp_path) -> CznBackend:
    return CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")}))


def test_czn_remake_moves_aside_creates_fresh_restores_game_files_then_installs(
    tmp_path, monkeypatch
):
    backend = _czn(tmp_path)
    prefix = backend.prefix()
    game_dir = backend.game_dir()
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_text("old game")
    (prefix / "drive_c" / "stale.dll").write_text("broken")
    events: list = []
    backend.ctx.emit = events.append
    calls: list[str] = []

    monkeypatch.setattr(backend, "_game_running", lambda: False)

    def fake_ensure() -> None:
        assert not prefix.exists(), "the old prefix is moved aside before the new one is built"
        calls.append("ensure")
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)

    def fake_install() -> None:
        calls.append("install")
        assert (backend.game_dir() / "game.exe").is_file(), "game files were restored"

    monkeypatch.setattr(backend, "_ensure_prefix", fake_ensure)
    monkeypatch.setattr(backend, "install", fake_install)

    backend.remake_prefix()

    assert calls == ["ensure", "install"]
    assert (backend.game_dir() / "game.exe").read_text() == "old game"
    backups = list(tmp_path.glob("prefix.broken-*"))
    assert backups, "the old prefix is kept under .broken-*"
    assert (backups[0] / "drive_c" / "stale.dll").is_file()
    assert ("step", "Moving the old Wine prefix aside") in [(e.kind, e.message) for e in events]
    done = [e.message for e in events if e.kind == "done"]
    assert any("old one was kept at" in message for message in done)
    assert str(backups[0]) in done[-1]


def test_czn_remake_rolls_back_when_a_step_fails(tmp_path, monkeypatch):
    backend = _czn(tmp_path)
    prefix = backend.prefix()
    game_dir = backend.game_dir()
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_text("old game")
    (prefix / "drive_c").mkdir(exist_ok=True)
    (prefix / "drive_c" / "stale.dll").write_text("broken")
    monkeypatch.setattr(backend, "_game_running", lambda: False)

    def failing_ensure() -> None:
        raise RuntimeError("wineboot exploded")

    monkeypatch.setattr(backend, "_ensure_prefix", failing_ensure)

    with pytest.raises(RuntimeError, match="wineboot exploded"):
        backend.remake_prefix()

    assert (backend.game_dir() / "game.exe").read_text() == "old game"
    assert (prefix / "drive_c" / "stale.dll").is_file()
    assert not list(tmp_path.glob("prefix.broken-*"))


def test_czn_remake_refuses_while_the_game_is_running(tmp_path, monkeypatch):
    backend = _czn(tmp_path)
    backend.prefix().mkdir()
    called: list[str] = []
    monkeypatch.setattr(backend, "_game_running", lambda: True)
    monkeypatch.setattr(prefixmove, "move_aside", lambda p: called.append("move"))

    with pytest.raises(BackendError) as exc:
        backend.remake_prefix()

    assert exc.value.code == 8
    assert called == []


# ---------------------------------------------------------------------------
# Arknights / Stella Sora
# ---------------------------------------------------------------------------


def _arknights(tmp_path, **profile) -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile))


def test_arknights_remake_uses_a_fresh_prefix_and_restores_the_game_folder(tmp_path, monkeypatch):
    backend = _arknights(tmp_path)
    prefix = backend.prefix()
    install_dir = backend.install_dir()
    install_dir.mkdir(parents=True)
    (install_dir / "Arknights.exe").write_text("old game")
    (prefix / "drive_c").mkdir(exist_ok=True)
    events: list = []
    backend.ctx.emit = events.append
    calls: list[str] = []
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [])
    monkeypatch.setattr(ArknightsBackend, "_ensure_default_runner", lambda self, strict=True: None)

    def fake_ensure() -> bool:
        assert not prefix.exists()
        calls.append("ensure")
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        return False

    monkeypatch.setattr(backend, "_ensure_prefix", fake_ensure)
    monkeypatch.setattr(
        backend, "_run_vcredist", lambda created=True: calls.append(f"vcredist:{created}")
    )

    backend.remake_prefix()

    assert calls == ["ensure", "vcredist:False"]
    assert (backend.install_dir() / "Arknights.exe").read_text() == "old game"
    backups = list(prefix.parent.glob("prefix.broken-*"))
    assert backups and (backups[0] / "drive_c").is_dir()
    done = [e.message for e in events if e.kind == "done"][-1]
    assert "old one was kept at" in done and str(backups[0]) in done


def test_arknights_remake_skips_vcredist_when_ensure_already_ran_it(tmp_path, monkeypatch):
    backend = _arknights(tmp_path)
    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    calls: list[str] = []
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [])
    monkeypatch.setattr(ArknightsBackend, "_ensure_default_runner", lambda self, strict=True: None)
    monkeypatch.setattr(backend, "_ensure_prefix", lambda: calls.append("ensure") or True)
    monkeypatch.setattr(
        backend, "_run_vcredist", lambda created=True: calls.append(f"vcredist:{created}")
    )

    backend.remake_prefix()

    assert calls == ["ensure"]


def test_arknights_remake_leaves_an_external_game_folder_alone(tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    (external / "Arknights.exe").write_text("outside")
    backend = _arknights(tmp_path, install_dir=str(external))
    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [])
    monkeypatch.setattr(ArknightsBackend, "_ensure_default_runner", lambda self, strict=True: None)
    monkeypatch.setattr(backend, "_ensure_prefix", lambda: True)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=True: None)

    backend.remake_prefix()

    assert (external / "Arknights.exe").read_text() == "outside"


def test_arknights_remake_rolls_back_on_failure(tmp_path, monkeypatch):
    backend = _arknights(tmp_path)
    prefix = backend.prefix()
    install_dir = backend.install_dir()
    install_dir.mkdir(parents=True)
    (install_dir / "Arknights.exe").write_text("old game")
    (prefix / "drive_c").mkdir(exist_ok=True)
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [])
    monkeypatch.setattr(ArknightsBackend, "_ensure_default_runner", lambda self, strict=True: None)

    def failing_ensure() -> bool:
        raise RuntimeError("wineboot exploded")

    monkeypatch.setattr(backend, "_ensure_prefix", failing_ensure)

    with pytest.raises(RuntimeError, match="wineboot exploded"):
        backend.remake_prefix()

    assert (backend.install_dir() / "Arknights.exe").read_text() == "old game"
    assert not list(prefix.parent.glob("prefix.broken-*"))


def test_arknights_remake_refuses_while_the_game_is_running(tmp_path, monkeypatch):
    backend = _arknights(tmp_path)
    backend.prefix().mkdir()
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [1234])

    with pytest.raises(BackendError) as exc:
        backend.remake_prefix()

    assert exc.value.code == 8
    assert backend.prefix().is_dir()


# ---------------------------------------------------------------------------
# verify hints
# ---------------------------------------------------------------------------


def test_czn_verify_points_at_the_settings_button_when_the_prefix_is_broken(tmp_path, monkeypatch):
    backend = _czn(tmp_path)
    monkeypatch.setattr(backend, "_differing_files", lambda: None)
    monkeypatch.setattr(backend, "_webview2_installed", lambda: True)
    monkeypatch.setattr(backend, "_vcredist_installed", lambda: True)
    monkeypatch.setattr(backend, "_session_info", lambda: None)

    report = backend.verify()

    assert report["checks"]["prefix"] is False
    assert report["message"].endswith("Rebuild the Wine prefix in Settings.")


def test_arknights_verify_points_at_the_settings_button_when_the_prefix_is_broken(tmp_path):
    backend = _arknights(tmp_path)

    report = backend.verify()

    assert report["checks"]["prefix"] is False
    assert report["message"].endswith("Rebuild the Wine prefix in Settings.")


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


def test_remake_prefix_is_parsed_with_the_usual_flags():
    from nadare import app

    args = app.build_parser().parse_args(
        ["remake-prefix", "-p", "game", "--yes", "--accept-account-notice"]
    )
    assert args.command == "remake-prefix"
    assert args.profile == "game"
    assert args.yes is True
    assert args.accept_account_notice is True
    assert "remake-prefix" in app.COMMANDS


def test_remake_prefix_requires_yes(tmp_path, monkeypatch, capsys):
    from nadare import app

    _, path = _cli_config(tmp_path)
    calls: list[str] = []

    class Fake:
        def requires_notice_ack(self) -> bool:
            return False

        def remake_prefix(self) -> None:
            calls.append("remake")

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: Fake())

    assert app.run_cli(["-c", str(path), "remake-prefix", "-p", "game"]) == 2
    assert calls == []
    err = capsys.readouterr()
    assert "old prefix" in (err.out + err.err).lower()


def test_remake_prefix_dispatches_with_yes(tmp_path, monkeypatch):
    from nadare import app

    _, path = _cli_config(tmp_path)
    calls: list[str] = []

    class Fake:
        def requires_notice_ack(self) -> bool:
            return False

        def remake_prefix(self) -> None:
            calls.append("remake")

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: Fake())

    assert app.run_cli(["-c", str(path), "remake-prefix", "-p", "game", "--yes"]) == 0
    assert calls == ["remake"]


# ---------------------------------------------------------------------------
# GUI worker
# ---------------------------------------------------------------------------


def test_the_worker_dispatches_remake_prefix(tmp_path, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui import workers

    assert "remake_prefix" in workers.OPERATIONS
    assert "remake_prefix" in workers.MUTATING
    calls: list[str] = []

    class Fake:
        def remake_prefix(self) -> None:
            calls.append("remake")

    monkeypatch.setattr(workers, "create", lambda *a, **k: Fake())
    workers.BackendWorker(Config.load(tmp_path / "config.json"), "p", "remake_prefix").run()
    assert calls == ["remake"]


# ---------------------------------------------------------------------------
# GUI
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


def test_settings_shows_the_rebuild_button_only_for_managed_prefixes(tmp_path, qapp):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    page = SettingsPage()
    try:
        managed = Config.load(tmp_path / "managed.json")
        managed.add_profile("czn", backend="stove-czn")
        page.load(managed, "czn")
        assert page.remake_prefix_btn.isVisibleTo(page)
        assert "not deleted" in page.remake_prefix_btn.toolTip()

        wine = Config.load(tmp_path / "wine.json")
        wine.add_profile("wine", backend="wine")
        page.load(wine, "wine")
        assert not page.remake_prefix_btn.isVisibleTo(page)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_rebuild_button_emits_the_profile(tmp_path, qapp):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    seen: list[str] = []
    page.remake_prefix_requested.connect(seen.append)
    try:
        page.load(config, "czn")
        page.remake_prefix_btn.click()
        assert seen == ["czn"]
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_remake_prefix_asks_first_and_does_nothing_on_no(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.core.config import Config
    from nadare.gui import main_window as mw

    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="stove-czn", title="Game")
    config.save()
    window = mw.MainWindow(config)
    opened: list[str] = []
    started: list[str] = []
    monkeypatch.setattr(window, "open_game", lambda profile: opened.append(profile))
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
        window._on_remake_prefix_requested("g")
        assert started == []
        assert opened == []
        assert asked and "Rebuild the Wine prefix for Game?" in asked[0]

        window._on_remake_prefix_requested("g")
        assert opened == ["g"]
        assert started == ["remake_prefix"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_czn_remake_failure_after_the_game_files_moved_puts_everything_back(
    tmp_path, monkeypatch
):
    backend = _czn(tmp_path)
    prefix = backend.prefix()
    game_dir = backend.game_dir()
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_text("old game")
    (prefix / "drive_c" / "stale.dll").write_text("broken")
    monkeypatch.setattr(backend, "_game_running", lambda: False)

    def fake_ensure() -> None:
        (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)

    def failing_install() -> None:
        raise RuntimeError("webview2 failed")

    monkeypatch.setattr(backend, "_ensure_prefix", fake_ensure)
    monkeypatch.setattr(backend, "install", failing_install)

    with pytest.raises(RuntimeError, match="webview2 failed"):
        backend.remake_prefix()

    assert (backend.game_dir() / "game.exe").read_text() == "old game"
    assert (prefix / "drive_c" / "stale.dll").read_text() == "broken"
    parked = list(prefix.parent.glob(f"{prefix.name}.failed-*"))
    assert not parked, "an emptied parked prefix is tidied up, not left behind"
