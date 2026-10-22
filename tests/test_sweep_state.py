# SPDX-License-Identifier: GPL-3.0-or-later
"""Robustness regressions for config/state handling (#365, #366, #371, #374..#376)."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core.config import Config

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import main_window as main_window_mod
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---- #365: a valid-JSON config with no profiles section must be quarantined --
def test_missing_profiles_lookalike_key_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = json.dumps({"schema": 1, "profile": {"game": {"backend": "wine"}}})
    path.write_text(original, encoding="utf-8")

    cfg = Config.load(path)

    assert cfg.notice and "has no profiles section" in cfg.notice
    backups = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert cfg.profiles == {}


def test_missing_profiles_with_unknown_top_level_keys_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "imported_games": []}), encoding="utf-8")

    cfg = Config.load(path)

    assert cfg.notice and "has no profiles section" in cfg.notice


@pytest.mark.parametrize("raw", [{}, {"schema": 1, "language": "en", "telemetry": False}])
def test_fresh_config_without_profiles_is_not_quarantined(tmp_path: Path, raw) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    cfg = Config.load(path)

    assert cfg.notice is None
    assert not list(tmp_path.glob("config.json.corrupt-*"))


# ---- #366: an unknown backend must get a plain, actionable explanation -------
def _window(tmp_path: Path, profiles: dict[str, dict]) -> MainWindow:
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles.items():
        config.add_profile(name, **values)
    config.save()
    return MainWindow(config)


def test_unknown_backend_status_message_is_plain(tmp_path: Path, qapp) -> None:
    window = _window(tmp_path, {"bad": {"backend": "arknights-yostar ", "title": "Bad"}})
    try:
        window._on_failed(
            "bad",
            "status",
            "unknown backend: 'arknights-yostar ' (have: arknights-yostar, wine)",
        )
        message = window._snapshots["bad"]["status"].message
        assert message == (
            "This game's type ('arknights-yostar ') is not recognised by this version of "
            "Nadare. Remove the game and add it again, or correct 'backend' in config.json."
        )
        assert "unknown backend" not in message
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_empty_backend_status_message_is_plain(tmp_path: Path, qapp) -> None:
    window = _window(tmp_path, {"bad": {"backend": "", "title": "Bad"}})
    try:
        window._on_failed("bad", "status", "profile 'bad' has no backend set")
        message = window._snapshots["bad"]["status"].message
        assert message.startswith("This game's type ('') is not recognised")
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #375: a local save must not discard another process's field edits ------
def test_save_merges_profile_fields_changed_by_another_process(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    seed = Config.load(path)
    seed.add_profile("game", backend="wine", title="Original", install_dir="/old")
    seed.save()

    first = Config.load(path)
    second = Config.load(path)

    first.profiles["game"]["title"] = "Local title"
    second.profiles["game"]["install_dir"] = "/disk"
    second.profiles["game"]["launch_mode"] = "from-disk"
    second.add_profile("other", backend="wine")
    second.save()

    first.save()

    disk = json.loads(path.read_text(encoding="utf-8"))
    profile = disk["profiles"]["game"]
    assert profile["title"] == "Local title"  # changed locally wins
    assert profile["install_dir"] == "/disk"  # unchanged locally takes disk
    assert profile["launch_mode"] == "from-disk"  # added on disk is kept
    assert set(disk["profiles"]) == {"game", "other"}


def test_save_keeps_a_key_removed_locally_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    seed = Config.load(path)
    seed.add_profile("game", backend="wine", title="Original", install_dir="/old")
    seed.save()

    first = Config.load(path)
    second = Config.load(path)

    first.profiles["game"].pop("title")
    second.profiles["game"]["title"] = "Disk title"
    second.profiles["game"]["install_dir"] = "/disk"
    second.save()

    first.save()

    profile = json.loads(path.read_text(encoding="utf-8"))["profiles"]["game"]
    assert "title" not in profile  # removed locally, even though disk changed it
    assert profile["install_dir"] == "/disk"


# ---- #376: external removals become visible to a running GUI ----------------
def test_disk_profile_names_follows_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("one", backend="wine")
    cfg.add_profile("two", backend="wine")
    cfg.save()
    assert cfg.disk_profile_names() == {"one", "two"}

    other = Config.load(path)
    other.remove_profile("two")
    other.save()
    assert cfg.disk_profile_names() == {"one"}

    path.unlink()
    assert cfg.disk_profile_names() is None  # unreadable is not "all removed"


def test_disk_profile_names_is_none_on_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("one", backend="wine")
    cfg.save()
    path.write_text("{not json", encoding="utf-8")
    assert cfg.disk_profile_names() is None


def test_activation_never_drops_profiles_when_the_file_is_unreadable(
    tmp_path: Path, qapp
) -> None:
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.save()
    window = MainWindow(config)
    try:
        path.write_text("{not json", encoding="utf-8")
        window._on_window_activated()
        assert "alpha" in window.config.profiles
        assert "alpha" not in window.config._removed
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_activation_drops_a_profile_removed_by_another_process(tmp_path: Path, qapp) -> None:
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.add_profile("beta", backend="wine", title="Beta")
    config.save()

    window = MainWindow(config)
    try:
        window.open_game("alpha")

        other = Config.load(path)
        other.remove_profile("alpha")
        other.save()

        # A locally added, not-yet-saved profile must survive the same check.
        window.config.add_profile("local", backend="wine", title="Local")
        window.refresh_library()

        window._on_window_activated()

        assert "alpha" not in window.config.profiles
        assert "local" in window.config.profiles
        assert "alpha" not in window.config._snapshot
        assert window._current_profile == ""
        assert window.side_status.text() == "This game was removed by another Nadare process."
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #371: activation and the timer re-probe the shown profile --------------
def test_activation_refreshes_status_only_when_not_busy(tmp_path: Path, qapp, monkeypatch) -> None:
    window = _window(tmp_path, {"alpha": {"backend": "wine", "title": "Alpha"}})
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.controller,
        "start",
        lambda profile, op, payload=None: started.append((profile, op)) or True,
    )
    try:
        window.open_game("alpha")
        started.clear()

        window._on_window_activated()
        assert started == [("alpha", "status")]

        started.clear()
        window._busy["alpha"] = "launch"
        window._on_window_activated()
        assert started == []
        assert main_window_mod.STATUS_REFRESH_INTERVAL_MS == 30_000
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #374: a deleted explicit Proton runner is not READY -------------------
def _ark_backend(tmp_path: Path, profile: dict | None = None) -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile))


def test_status_errors_when_explicit_runner_is_gone(tmp_path: Path) -> None:
    backend = _ark_backend(tmp_path, {"runner": str(tmp_path / "missing-runner")})
    write_exe(backend.prefix() / ak.DEFAULT_INSTALL_SUBDIR / "game launcher.exe", "MZ")

    status = backend.status()

    assert status.state == "error"
    assert status.message == (
        "Game files are present but the Proton runner is missing: "
        "choose or install a runner in Settings."
    )


def test_status_stays_installed_for_the_default_runner(tmp_path: Path) -> None:
    backend = _ark_backend(tmp_path)
    write_exe(backend.prefix() / ak.DEFAULT_INSTALL_SUBDIR / "game launcher.exe", "MZ")

    assert backend.status().state == "installed"
