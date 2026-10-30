# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for Yostar direct-download modes (Arknights + Stella Sora).

Everything is offline: the installer, the downloader and Wine are replaced with
fakes; the manifest is a local file and the only salt used is ``"test-salt"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import yostar
from nadare.core.backends import BackendError, get, kinds
from nadare.core.events import Cancelled


@pytest.fixture(autouse=True)
def _no_real_vcredist(monkeypatch):
    """The VC++ step always runs now; keep it out of tests about other things."""
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _hermetic_runners(monkeypatch):
    """No test may read the developer's real runners or download one."""
    monkeypatch.setattr(wg.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise wg.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(wg.runners, "ensure_pinned", offline)


def _manifest_file(tmp_path: Path, tag: str, exe: str = "Arknights.exe") -> Path:
    payload = {
        "game_tag": tag,
        "version": "1.2.3",
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [{"path": exe, "size": 2, "crc64": yostar.crc64(b"MZ")}],
    }
    p = tmp_path / f"{tag}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _ak(tmp_path: Path, profile: dict | None = None) -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile, "ak"))


def _stella(tmp_path: Path, profile: dict | None = None) -> StellaSoraBackend:
    return StellaSoraBackend(make_context(tmp_path, StellaSoraBackend.kind, profile, "stella"))


# ---------------------------------------------------------------------------
# registration / mode selection
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_stellasora_registered(self):
        assert StellaSoraBackend.kind in kinds()
        assert get(StellaSoraBackend.kind) is StellaSoraBackend


class TestArknightsMode:
    def test_official_by_default(self, tmp_path):
        backend = _ak(tmp_path)
        assert backend.direct_mode() is False
        assert backend.status().data["mode"] == "official"

    def test_direct_when_manifest_configured(self, tmp_path):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        backend = _ak(tmp_path, {"manifest_file": str(manifest)})
        assert backend.direct_mode() is True
        assert backend.status().data["mode"] == "direct"

    def test_env_var_selects_direct(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        monkeypatch.setenv("YOSTAR_MANIFEST_INDEX", str(manifest))
        assert _ak(tmp_path).direct_mode() is True

    def test_install_direct_uses_sync(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        backend = _ak(tmp_path, {"manifest_file": str(manifest)})
        seen: list = []

        def fake_sync(man, install_dir, emit=None, cancel=None, **kw):
            seen.append((man.game_tag, Path(install_dir)))
            return {"downloaded": 1, "skipped": 0}

        monkeypatch.setattr(ak.yostar, "sync", fake_sync)

        def no_installer(*a, **k):
            raise AssertionError("direct mode must not run the installer")

        monkeypatch.setattr(WineGameBackend, "_download_installer", no_installer)
        backend.install()
        assert seen and seen[0][0] == "Arknights_EN"

    def test_update_direct_uses_sync(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        backend = _ak(tmp_path, {"manifest_file": str(manifest)})
        target = backend.install_dir() / "Arknights.exe"
        write_exe(target, "MZ")
        seen: list = []
        monkeypatch.setattr(
            ak.yostar,
            "sync",
            lambda man, d, emit=None, cancel=None, **k: (
                seen.append(man.version) or {"downloaded": 0, "skipped": 0}
            ),
        )
        backend.update()
        assert seen == ["1.2.3"]

    def test_launch_direct_runs_exe_in_install_dir(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        backend = _ak(tmp_path, {"manifest_file": str(manifest), "wine_binary": "/usr/bin/wine"})
        exe = write_exe(backend.install_dir() / "Arknights.exe", "MZ")
        captured: list[list[str]] = []

        def fake_stream(argv, env, on_line=None, cancel=None):
            captured.append(list(argv))
            return 0

        monkeypatch.setattr(wg, "stream", fake_stream)
        assert backend.launch() == 0
        assert captured[0][0] == "/usr/bin/wine"
        assert captured[0][1] == str(exe)

    def test_official_install_still_emits_finish_step(self, tmp_path, monkeypatch):
        installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
        backend = _ak(
            tmp_path,
            {
                "wine_binary": "/usr/bin/wine",
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
            },
        )
        monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
        monkeypatch.setattr(wg, "stream", lambda *a, **k: 0)
        write_exe(backend.install_dir() / "game launcher.exe", "MZ")
        messages: list[str] = []
        original = backend.ctx.emit

        def emit(event):
            messages.append(event.message)
            original(event)

        backend.ctx.emit = emit
        backend.install()
        assert any("installer window" in m for m in messages)

    def test_download_installer_checks_known_size(self, tmp_path, monkeypatch):
        backend = _ak(tmp_path, {"installer_url": "https://example.test/setup.exe"})
        calls: list[tuple[Path, int]] = []
        monkeypatch.setattr(
            wg.download,
            "remote_size",
            lambda url, **k: 500 * (1 << 20),
        )
        monkeypatch.setattr(
            wg.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )

        def fake_download(url, dest, **kwargs):
            return wg.download.DownloadResult(dest, 1024, "a", "b")

        monkeypatch.setattr(wg.download, "download", fake_download)
        backend._download_installer("https://example.test/setup.exe")
        assert calls == [(backend.cache_dir(), 500 * (1 << 20))]


# ---------------------------------------------------------------------------
# Stella Sora
# ---------------------------------------------------------------------------
class TestStellaSora:
    def test_install_without_manifest_errors_cleanly(self, tmp_path):
        backend = _stella(tmp_path)
        with pytest.raises(BackendError) as excinfo:
            backend.install()
        assert "manifest" in str(excinfo.value).lower()

    def test_update_without_manifest_errors_cleanly(self, tmp_path):
        backend = _stella(tmp_path)
        with pytest.raises(BackendError) as excinfo:
            backend.update()
        assert "manifest" in str(excinfo.value).lower()

    def test_launch_without_manifest_errors_cleanly(self, tmp_path):
        backend = _stella(tmp_path)
        with pytest.raises(BackendError):
            backend.launch()

    def test_install_syncs_into_install_dir(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "StellaSora_EN", exe="StellaSora.exe")
        backend = _stella(tmp_path, {"manifest_file": str(manifest)})
        seen: list = []
        monkeypatch.setattr(
            ak.yostar,
            "sync",
            lambda man, d, emit=None, cancel=None, **k: (
                seen.append(Path(d)) or {"downloaded": 0, "skipped": 0}
            ),
        )
        backend.install()
        assert seen == [backend.install_dir()]

    def test_launch_uses_stellasora_exe(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "StellaSora_EN", exe="StellaSora.exe")
        backend = _stella(
            tmp_path, {"manifest_file": str(manifest), "wine_binary": "/usr/bin/wine"}
        )
        exe = write_exe(backend.install_dir() / "StellaSora.exe", "MZ")
        captured: list[list[str]] = []
        monkeypatch.setattr(
            wg,
            "stream",
            lambda argv, env, on_line=None, cancel=None: (captured.append(list(argv)), 0)[1],
        )
        assert backend.launch() == 0
        assert captured[0] == ["/usr/bin/wine", str(exe)]

    def test_install_sets_up_the_default_runner_without_failing_on_it(self, tmp_path, monkeypatch):
        """The audit's blocker 1: this override replaced the base install()."""
        manifest = _manifest_file(tmp_path, "StellaSora_EN", exe="StellaSora.exe")
        backend = _stella(tmp_path, {"manifest_file": str(manifest)})
        monkeypatch.setattr(wg.shutil, "which", lambda name: None)
        calls: list = []
        monkeypatch.setattr(
            wg.runners, "ensure_pinned", lambda *a, **k: calls.append(1) or tmp_path
        )
        monkeypatch.setattr(ak.yostar, "sync", lambda *a, **k: {"downloaded": 0, "skipped": 0})
        backend.install()
        assert calls == [1]

        def offline(*_a, **_k):
            raise wg.download.DownloadError("offline")

        monkeypatch.setattr(wg.runners, "ensure_pinned", offline)
        backend.install()  # a failed runner download does not abort the file sync

    def test_launch_sets_up_the_runner_only_for_an_installed_game(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "StellaSora_EN", exe="StellaSora.exe")
        backend = _stella(tmp_path, {"manifest_file": str(manifest)})
        monkeypatch.setattr(wg.shutil, "which", lambda name: None)
        monkeypatch.setattr(wg.runners, "ensure_pinned", lambda *a, **k: pytest.fail("downloaded"))
        with pytest.raises(BackendError, match="not found"):
            backend.launch()  # not installed: no 320 MB download first

        write_exe(backend.install_dir() / "StellaSora.exe", "MZ")
        calls: list = []
        monkeypatch.setattr(
            wg.runners, "ensure_pinned", lambda *a, **k: calls.append(1) or tmp_path
        )
        monkeypatch.setattr(wg, "stream", lambda *a, **k: 0)
        monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: [])
        # CI runners have no wine; the test must not depend on one being installed.
        monkeypatch.setattr(wg, "first_wine", lambda: Path("/usr/bin/wine"))
        backend.launch()
        assert calls == [1]

    def test_exe_name_override(self, tmp_path, monkeypatch):
        manifest = _manifest_file(tmp_path, "StellaSora_EN")
        backend = _stella(
            tmp_path,
            {
                "manifest_file": str(manifest),
                "wine_binary": "/usr/bin/wine",
                "exe_name": "bin/StellaSora.exe",
            },
        )
        exe = write_exe(backend.install_dir() / "bin" / "StellaSora.exe", "MZ")
        captured: list[list[str]] = []
        monkeypatch.setattr(
            wg,
            "stream",
            lambda argv, env, on_line=None, cancel=None: (captured.append(list(argv)), 0)[1],
        )
        backend.launch()
        assert captured[0][1] == str(exe)

    def test_status_reports_direct_mode(self, tmp_path):
        manifest = _manifest_file(tmp_path, "StellaSora_EN")
        backend = _stella(tmp_path, {"manifest_file": str(manifest)})
        assert backend.status().data["mode"] == "direct"


class TestDisplayDefault:
    """Arknights stays on native Wayland; Stella Sora defaults to XWayland
    because its gacha swipe does not register under the Wayland driver."""

    @staticmethod
    def _env(backend, monkeypatch, tmp_path):

        monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
        monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
        monkeypatch.setattr(wg.shutil, "which", lambda name: "/usr/bin/umu-run")
        return backend._prefix_command(["game.exe"])[1]["PROTON_ENABLE_WAYLAND"]

    @pytest.mark.parametrize(
        ("display", "expected"),
        [(None, "0"), ("auto", "0"), ("x11", "0"), ("xwayland", "0"), ("wayland", "1")],
    )
    def test_stellasora(self, tmp_path, monkeypatch, display, expected):
        backend = _stella(tmp_path, {"display": display} if display else {})
        assert self._env(backend, monkeypatch, tmp_path) == expected

    @pytest.mark.parametrize(
        ("display", "expected"),
        [(None, "1"), ("auto", "1"), ("x11", "0"), ("wayland", "1")],
    )
    def test_arknights_unchanged(self, tmp_path, monkeypatch, display, expected):
        from nadare.backends.arknights import ArknightsBackend

        profile = {"display": display} if display else {}
        backend = ArknightsBackend(make_context(tmp_path, ArknightsBackend.kind, profile, "ak"))
        assert self._env(backend, monkeypatch, tmp_path) == expected


# ---------------------------------------------------------------------------
# direct install completion / status / verify
# ---------------------------------------------------------------------------
def _multi_manifest(tmp_path: Path, tag: str, files: dict[str, bytes]) -> Path:
    payload = {
        "game_tag": tag,
        "version": "2.0.0",
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": path, "size": len(data), "crc64": yostar.crc64(data)}
            for path, data in files.items()
        ],
    }
    p = tmp_path / f"{tag}-multi.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _write_files(root: Path, files: dict[str, bytes], stop_after: str | None = None) -> None:
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path == stop_after:
            raise Cancelled("cancelled")


class TestDirectInstallCompletion:
    GAME_FILES = {"Arknights.exe": b"MZ", "data/big.pak": b"BIG"}

    def _backend(self, tmp_path: Path) -> tuple[ArknightsBackend, Path]:
        manifest = _multi_manifest(tmp_path, "Arknights_EN", self.GAME_FILES)
        return _ak(tmp_path, {"manifest_file": str(manifest)}), manifest

    def test_interrupted_install_is_not_reported_installed(self, tmp_path, monkeypatch):
        backend, _manifest = self._backend(tmp_path)

        def interrupted(man, d, emit=None, cancel=None, **kw):
            _write_files(Path(d), self.GAME_FILES, stop_after="Arknights.exe")
            return {"downloaded": 1, "skipped": 0}

        monkeypatch.setattr(ak.yostar, "sync", interrupted)
        with pytest.raises(Cancelled):
            backend.install()

        status = backend.status()
        assert status.state == "not_installed"
        assert "incomplete" in status.message.lower()
        report = backend.verify()
        assert report["verified"] is False
        assert "all checks passed" not in report["message"].lower()

    def test_interrupted_update_is_not_reported_installed(self, tmp_path, monkeypatch):
        backend, _manifest = self._backend(tmp_path)
        monkeypatch.setattr(
            ak.yostar,
            "sync",
            lambda man, d, emit=None, cancel=None, **kw: (
                _write_files(Path(d), self.GAME_FILES)
                or {"downloaded": 2, "skipped": 0}
            ),
        )
        backend.install()
        assert backend.status().state == "installed"

        def interrupted(man, d, emit=None, cancel=None, **kw):
            _write_files(Path(d), {"Arknights.exe": b"MZ2"}, stop_after="Arknights.exe")
            return {"downloaded": 1, "skipped": 0}

        monkeypatch.setattr(ak.yostar, "sync", interrupted)
        with pytest.raises(Cancelled):
            backend.update()

        status = backend.status()
        assert status.state == "not_installed"
        assert "incomplete" in status.message.lower()

    def test_completed_install_clears_the_marker_and_status_uses_the_launch_target(
        self, tmp_path, monkeypatch
    ):
        backend, _manifest = self._backend(tmp_path)
        monkeypatch.setattr(
            ak.yostar,
            "sync",
            lambda man, d, emit=None, cancel=None, **kw: (
                _write_files(Path(d), self.GAME_FILES)
                or {"downloaded": 2, "skipped": 0}
            ),
        )
        backend.install()

        assert not (backend.install_dir() / ak.INSTALL_MARKER).exists()
        status = backend.status()
        assert status.state == "installed"
        assert status.data["exe"] == str(backend.direct_target())
        assert backend.verify()["verified"] is True

    def test_verify_names_a_missing_game_file(self, tmp_path, monkeypatch):
        backend, _manifest = self._backend(tmp_path)
        monkeypatch.setattr(
            ak.yostar,
            "sync",
            lambda man, d, emit=None, cancel=None, **kw: (
                _write_files(Path(d), self.GAME_FILES)
                or {"downloaded": 2, "skipped": 0}
            ),
        )
        backend.install()
        (backend.install_dir() / "data" / "big.pak").unlink()

        report = backend.verify()
        assert report["verified"] is False
        assert report["checks"]["files"] is False
        assert "big.pak" in report["message"]

    def test_status_ignores_an_old_launcher_elsewhere_in_the_prefix(self, tmp_path):
        manifest = _manifest_file(tmp_path, "Arknights_EN")
        new_dir = tmp_path / "new-install"
        backend = _ak(
            tmp_path,
            {
                "manifest_file": str(manifest),
                "install_dir": str(new_dir),
            },
        )
        write_exe(
            backend.prefix() / ak.DEFAULT_INSTALL_SUBDIR / "Arknights.exe",
            "MZ",
        )

        status = backend.status()
        assert status.state == "not_installed"
        assert status.data["exe"] == str(backend.direct_target())

        write_exe(backend.direct_target(), "MZ")
        assert backend.status().state == "installed"
        (backend.install_dir() / ak.INSTALL_MARKER).write_text("x")
        status = backend.status()
        assert status.state == "not_installed"
        assert "incomplete" in status.message.lower()


class TestLegacyDirectInstall:
    def test_install_from_before_the_marker_still_counts_as_installed(self, tmp_path):
        manifest = _multi_manifest(tmp_path, "Arknights_EN", {"Arknights.exe": b"MZ"})
        backend = _ak(tmp_path, {"manifest_file": str(manifest)})
        _write_files(backend.install_dir(), {"Arknights.exe": b"MZ"})

        assert backend.status().state == "installed"
        assert backend.verify()["verified"] is True

    def test_verify_offline_does_not_blame_the_files(self, tmp_path):
        backend = _ak(tmp_path, {"manifest_file": str(tmp_path / "missing.json")})
        _write_files(backend.install_dir(), {"Arknights.exe": b"MZ"})

        report = backend.verify()
        assert report["verified"] is True
        assert "could not compare" in report["message"]
