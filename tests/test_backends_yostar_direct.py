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
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core import yostar
from nadare.core.backends import BackendError, get, kinds


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

        monkeypatch.setattr(ArknightsBackend, "_download_installer", no_installer)
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

        monkeypatch.setattr(ak, "stream", fake_stream)
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
        monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer)
        monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)
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
            ak.download,
            "remote_size",
            lambda url, **k: 500 * (1 << 20),
        )
        monkeypatch.setattr(
            ak.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )

        def fake_download(url, dest, **kwargs):
            return ak.download.DownloadResult(dest, 1024, "a", "b")

        monkeypatch.setattr(ak.download, "download", fake_download)
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
            ak,
            "stream",
            lambda argv, env, on_line=None, cancel=None: (captured.append(list(argv)), 0)[1],
        )
        assert backend.launch() == 0
        assert captured[0] == ["/usr/bin/wine", str(exe)]

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
            ak,
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
        from nadare.backends import arknights as ak

        monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
        monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
        monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/umu-run")
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
