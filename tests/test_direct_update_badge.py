# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct-mode games advertise updates from a cached published manifest version.

Everything here is offline: manifests are local files and ``yostar.sync`` is
replaced with a fake, so no test touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core import yostar
from nadare.core.backends import Status
from nadare.core.config import Config


@pytest.fixture(autouse=True)
def _no_real_vcredist(monkeypatch):
    monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _hermetic_runners(monkeypatch):
    monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise ak.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(ak.runners, "ensure_pinned", offline)


def _manifest_file(tmp_path: Path, tag: str = "Arknights_EN", version: str = "1.2.3") -> Path:
    payload = {
        "game_tag": tag,
        "version": version,
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [{"path": "Arknights.exe", "size": 2, "crc64": yostar.crc64(b"MZ")}],
    }
    path = tmp_path / f"{tag}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ak(tmp_path: Path, profile: dict | None = None) -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile, "ak"))


def _installed(tmp_path: Path, profile: dict | None = None) -> ArknightsBackend:
    backend = _ak(tmp_path, profile)
    write_exe(backend.direct_target(), "MZ")
    return backend


class TestVersionRecord:
    def test_round_trips(self, tmp_path):
        backend = _ak(tmp_path)
        backend._write_version_file("1.2.3", "Arknights_EN")
        record = backend._read_version_file()
        assert record is not None
        assert record["version"] == "1.2.3"
        assert record["game_tag"] == "Arknights_EN"
        assert record["synced_at"]

    def test_missing_is_none(self, tmp_path):
        assert _ak(tmp_path)._read_version_file() is None

    @pytest.mark.parametrize("garbage", ["{", "[]", '{"version": 3}', "null"])
    def test_garbage_is_none(self, tmp_path, garbage):
        backend = _ak(tmp_path)
        path = backend.install_dir() / ak.VERSION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(garbage, encoding="utf-8")
        assert backend._read_version_file() is None


class TestStatusUpdateBadge:
    def _status(self, tmp_path: Path, recorded: str | None, latest: str | None):
        backend = _installed(
            tmp_path, {"manifest_file": str(_manifest_file(tmp_path, version="9.9.9"))}
        )
        if recorded is not None:
            backend._write_version_file(recorded, "Arknights_EN")
        if latest is not None:
            backend._write_latest(latest)
        return backend, backend.status()

    def test_newer_published_version_marks_update(self, tmp_path):
        _backend, status = self._status(tmp_path, "1.0.0", "1.1.0")
        assert status.state == "installed"
        assert status.data["version"] == "1.0.0"
        assert status.data["update_available"] is True

    def test_equal_versions_do_not(self, tmp_path):
        _backend, status = self._status(tmp_path, "1.0.0", "1.0.0")
        assert status.data["update_available"] is False

    def test_missing_cache_does_not(self, tmp_path):
        _backend, status = self._status(tmp_path, "1.0.0", None)
        assert status.data["version"] == "1.0.0"
        assert status.data["update_available"] is False

    def test_status_never_fetches_the_manifest(self, tmp_path, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("status() must not touch the network")

        monkeypatch.setattr(ak.yostar, "load_manifest", boom)
        _backend, status = self._status(tmp_path, "1.0.0", "2.0.0")
        assert status.data["update_available"] is True


class TestCheckUpdate:
    def test_writes_the_cache_from_the_manifest(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path, {"manifest_file": str(_manifest_file(tmp_path, version="9.9.9"))})
        backend.check_update()
        latest = backend._read_latest()
        assert latest is not None
        assert latest["version"] == "9.9.9"
        assert latest["checked_at"] > 0

    def test_fresh_cache_is_not_fetched_again(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path, {"manifest_file": str(_manifest_file(tmp_path))})
        calls: list[int] = []
        real = backend._load_manifest

        def counting():
            calls.append(1)
            return real()

        backend._load_manifest = counting
        backend.check_update()
        backend.check_update()
        assert calls == [1]

    def test_errors_are_swallowed_and_leave_the_cache_alone(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path, {"manifest_file": str(tmp_path / "missing.json")})
        backend.check_update()  # no exception, no cache
        assert backend._read_latest() is None

    def test_respects_the_global_setting(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path, {"manifest_file": str(_manifest_file(tmp_path))})
        backend.ctx.config.data["check_updates"] = False

        def boom(*_a, **_k):
            raise AssertionError("disabled setting must not fetch")

        monkeypatch.setattr(ak.yostar, "load_manifest", boom)
        backend.check_update()
        assert backend._read_latest() is None

    def test_official_mode_is_a_no_op(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path)
        monkeypatch.setattr(backend, "_load_manifest", lambda: pytest.fail("fetched"))
        backend.check_update()
        assert backend._read_latest() is None


class TestSyncRecordsVersions:
    def test_successful_sync_records_version_and_latest(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, version="3.0.0")
        backend = _installed(tmp_path, {"manifest_file": str(manifest)})
        backend._write_latest("2.0.0")
        monkeypatch.setattr(
            ak.yostar, "sync", lambda *a, **k: {"downloaded": 0, "skipped": 0}
        )

        backend.update()

        record = backend._read_version_file()
        assert record is not None
        assert record["version"] == "3.0.0"
        assert record["game_tag"] == "Arknights_EN"
        assert backend._read_latest()["version"] == "3.0.0"
        assert backend.status().data["update_available"] is False


class TestGuiWiring:
    def test_worker_dispatches_check_update(self, tmp_path, monkeypatch):
        from nadare.gui import workers

        assert "check_update" in workers.OPERATIONS
        assert "check_update" not in workers.MUTATING
        config = Config.load(tmp_path / "config.json")
        config.add_profile("p", backend="wine")
        calls: list[int] = []

        class FakeBackend:
            def check_update(self):
                calls.append(1)

        monkeypatch.setattr(workers, "create", lambda *a, **k: FakeBackend())
        workers.BackendWorker(config, "p", "check_update").run()
        assert calls == [1]

    def test_game_view_shows_update_primary(self, qapp):
        from nadare.gui.game_view import GameView

        view = GameView()
        try:
            view.set_status(
                Status(
                    state="installed",
                    version="1.0.0",
                    data={"mode": "direct", "version": "1.0.0", "update_available": True},
                )
            )
            assert view.primary.text() == "UPDATE"
            assert view.primary.isEnabled()
        finally:
            view.deleteLater()
            qapp.processEvents()

    def _window(self, tmp_path):
        from nadare.gui.main_window import MainWindow

        config = Config.load(tmp_path / "config.json")
        config.add_profile("sample", backend="stellasora-yostar")
        return MainWindow(config), config

    @staticmethod
    def _record_start(monkeypatch, window, seen):
        def start(profile, op, payload=None, silent=False):
            seen.append(op)
            return True

        monkeypatch.setattr(window, "_start", start)

    def test_main_window_does_not_request_when_setting_is_off(self, tmp_path, qapp, monkeypatch):
        window, config = self._window(tmp_path)
        try:
            config.data["check_updates"] = False
            window._current_profile = "sample"
            window._update_checked.clear()
            seen: list[str] = []
            self._record_start(monkeypatch, window, seen)
            window._after_status("sample")
            assert "check_update" not in seen
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_main_window_requests_once_per_six_hours(self, tmp_path, qapp, monkeypatch):
        window, config = self._window(tmp_path)
        try:
            config.data["check_updates"] = True
            window._current_profile = "sample"
            window._update_checked.clear()
            seen: list[str] = []
            self._record_start(monkeypatch, window, seen)
            window._after_status("sample")
            window._after_status("sample")
            assert seen.count("check_update") == 1
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


@pytest.fixture(scope="module")
def qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as exc:  # noqa: BLE001 - no GUI extras in this interpreter
        pytest.skip(f"PyQt6 is not importable: {exc}")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
