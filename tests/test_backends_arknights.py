# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Arknights (Yostar) backend's Wine set-up and the shared
installer/launcher route.

Nothing here opens a socket or starts Wine: the installer/download paths are
monkeypatched, and every path lives under the isolated ``tmp_path`` XDG tree.

#1452 retired the launcher route for Arknights itself; the route still serves the
``launcher-*`` kinds, so these tests drive it through :class:`OfficialRouteBackend`
(Arknights' set-up with the old no-manifest switch). Arknights' own direct-only
behaviour is in ``test_yostar_launcher_retired_1452.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _official_route import OfficialRouteBackend
from _parts_patch import patch_everywhere

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core.backends import BackendError, BackendUnsupported, get, kinds


def _backend(tmp_path: Path, profile: dict | None = None, name: str = "test") -> ArknightsBackend:
    return OfficialRouteBackend(make_context(tmp_path, ak.KIND, profile, name))


def _install_launcher(
    prefix: Path,
    name: str = "game launcher.exe",
    subdir: Path = ak.DEFAULT_INSTALL_SUBDIR,
) -> Path:
    return write_exe(prefix / subdir / name, "MZ")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_kind_is_registered(self):
        assert ak.KIND in kinds()
        assert get(ak.KIND) is ArknightsBackend


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_not_installed(self, tmp_path):
        backend = _backend(tmp_path)
        status = backend.status()
        assert status.state == "not_installed"
        assert status.running is False
        assert status.data["exe"] == ""
        assert status.data["prefix"] == str(backend.prefix())

    def test_installed_at_default_path(self, tmp_path):
        backend = _backend(tmp_path)
        _install_launcher(backend.prefix())
        status = backend.status()
        assert status.state == "installed"
        assert status.running is False
        assert status.data["exe"].endswith("game launcher.exe")

    def test_running_when_process_matches(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path)
        _install_launcher(backend.prefix())
        patch_everywhere(monkeypatch, wg, "scan_pids", lambda patterns: [4242])
        status = backend.status()
        assert status.state == "running"
        assert status.running is True

    def test_explicit_exe_name_resolves_against_install_dir(self, tmp_path):
        game = tmp_path / "game"
        exe = write_exe(game / "sub" / "client.exe", "MZ")
        backend = _backend(tmp_path, {"install_dir": str(game), "exe_name": "sub/client.exe"})
        status = backend.status()
        assert status.state == "installed"
        assert status.data["exe"] == str(exe)


# ---------------------------------------------------------------------------
# installer URL resolution
# ---------------------------------------------------------------------------
class TestInstallerUrl:
    def test_profile_override_wins(self, tmp_path):
        url = "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe"
        backend = _backend(tmp_path, {"installer_url": url})
        assert backend.installer_url() == url
        assert backend.require_installer_url() == url

    def test_override_on_untrusted_host_is_refused(self, tmp_path):
        backend = _backend(tmp_path, {"installer_url": "https://example.test/Arknights-setup.exe"})
        with pytest.raises(BackendError):
            backend.require_installer_url()

    def test_sha256_pin_allows_an_untrusted_host(self, tmp_path):
        url = "https://example.test/Arknights-setup.exe"
        digest = "a" * 64
        backend = _backend(tmp_path, {"installer_url": url, "installer_sha256": digest})
        assert backend.require_installer_url() == url
        assert backend._installer_sha256() == digest

    def test_malformed_sha256_is_rejected(self, tmp_path):
        backend = _backend(tmp_path, {"installer_sha256": "not-a-hash"})
        with pytest.raises(BackendError):
            backend._installer_sha256()

    def test_default_used_when_unset(self, tmp_path):
        backend = _backend(tmp_path)
        assert backend.installer_url() == wg.DEFAULT_INSTALLER_URL
        assert wg.DEFAULT_INSTALLER_URL.endswith(".exe")

    @pytest.mark.parametrize(
        ("url", "ok"),
        [
            ("https://a/b/setup.exe", True),
            ("https://a/b/setup.EXE", True),
            ("http://a/b/setup.exe", False),
            ("https://a/b/page.html", False),
            ("https://a/b/", False),
            ("ftp://a/b/setup.exe", False),
            ("", False),
        ],
    )
    def test_direct_exe_detection(self, url, ok):
        assert ArknightsBackend._is_direct_https_exe(url) is ok

    def test_non_exe_override_is_a_clear_error(self, tmp_path):
        backend = _backend(tmp_path, {"installer_url": "https://www.arknights.global/"})
        with pytest.raises(BackendError) as excinfo:
            backend.require_installer_url()
        assert "installer_url" in str(excinfo.value)

    def test_api_resolution(self, tmp_path, monkeypatch):
        url = "https://launcher-pkg-ark-en.yo-star.com/Arknights-new-setup.exe"
        payload = {"data": {"rows": [{"client": url}]}}
        monkeypatch.setattr(wg.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == url

    def test_api_url_on_untrusted_host_falls_back_to_pinned(self, tmp_path, monkeypatch):
        payload = {"data": {"rows": [{"client": "https://cdn.test/Arknights-setup.exe"}]}}
        monkeypatch.setattr(wg.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == wg.DEFAULT_INSTALLER_URL

    def test_api_failure_falls_back_to_pinned(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise wg.download.DownloadError("offline")

        monkeypatch.setattr(wg.download, "fetch_json", boom)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == wg.DEFAULT_INSTALLER_URL

    def test_api_with_html_link_is_ignored(self, tmp_path, monkeypatch):
        payload = {"data": {"rows": [{"client": "https://www.arknights.global/"}]}}
        monkeypatch.setattr(wg.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == wg.DEFAULT_INSTALLER_URL


# ---------------------------------------------------------------------------
# launch command construction
# ---------------------------------------------------------------------------
class TestBuildLaunch:
    def test_bare_wine_and_args(self, tmp_path):
        prefix = tmp_path / "prefix"
        exe = _install_launcher(prefix)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "launch_args": ["--skip-update"],
            },
        )
        argv, env, target = backend.build_launch()
        assert argv == ["/usr/bin/wine", str(exe), "--skip-update"]
        assert env["WINEPREFIX"] == str(prefix)
        assert target == exe

    def test_string_launch_args_are_shlex_split(self, tmp_path):
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "launch_args": "--name 'two words'",
            },
        )
        argv, *_ = backend.build_launch()
        assert argv[-2:] == ["--name", "two words"]

    def test_proton_runner_uses_umu(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        exe = _install_launcher(prefix)
        proton = tmp_path / "Proton-CachyOS"
        monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(
            wg.shutil, "which",
            lambda name, *_a, **_k: "/opt/umu-run" if name == "umu-run" else None,
        )
        backend = _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"})
        argv, env, target = backend.build_launch()
        assert argv == ["/opt/umu-run", str(exe)]
        assert env["PROTONPATH"] == str(proton)
        assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix)
        # #36: this names the Steam client install, never the game prefix.
        assert "STEAM_COMPAT_CLIENT_INSTALL_PATH" not in env
        assert target == exe

    def test_flatpak_without_umu_runs_proton_directly(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        exe = _install_launcher(prefix)
        proton = tmp_path / "Proton-CachyOS"
        proton.mkdir()
        (proton / "proton").write_text("")
        monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: None)
        monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: True)
        backend = _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"})
        argv, env, target = backend.build_launch()
        assert argv == [str(proton / "proton"), "run", str(exe)]
        assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix)
        assert (prefix / "pfx").is_symlink()
        assert target == exe

    def _proton_without_umu(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        proton = tmp_path / "Proton"
        proton.mkdir()
        (proton / "proton").write_text("")
        monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: None)
        monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
        return _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"}), proton

    def test_proton_runner_without_umu_never_runs_proton_directly(self, tmp_path, monkeypatch):
        """#1338 (user): with the 32-bit libraries and no umu this ran `proton run`, which
        skipped every per-game fix. Now Nadare's own umu is set up, or the launch explains."""
        backend, _proton = self._proton_without_umu(tmp_path, monkeypatch)
        monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: [])
        monkeypatch.setattr(wg.umu, "resolve", lambda *a, **k: None)
        with pytest.raises(BackendError, match="umu"):
            backend.build_launch()

    def test_proton_without_umu_and_without_host_libs_explains_what_to_install(
        self, tmp_path, monkeypatch
    ):
        backend, _proton = self._proton_without_umu(tmp_path, monkeypatch)
        monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
        with pytest.raises(BackendError):
            backend.build_launch()

    def test_plain_wine_suppresses_the_mono_and_gecko_prompts(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        wine = write_exe(tmp_path / "wine", "")
        backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": str(wine)})
        _argv, env, _target = backend.build_launch()
        assert "mscoree,mshtml=" in env["WINEDLLOVERRIDES"]

    def test_extra_env_is_merged(self, tmp_path):
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "extra_env": {"DXVK_HUD": "fps"},
            },
        )
        _argv, env, _target = backend.build_launch()
        assert env["DXVK_HUD"] == "fps"

    def test_relative_exe_is_resolved_against_install_dir(self, tmp_path):
        game = tmp_path / "game"
        exe = write_exe(game / "bin" / "client.exe", "MZ")
        backend = _backend(
            tmp_path,
            {
                "install_dir": str(game),
                "exe_name": "bin/client.exe",
                "wine_binary": "/usr/bin/wine",
            },
        )
        argv, _env, target = backend.build_launch()
        assert target == exe
        assert argv[1] == str(exe)

    def test_whitespace_wine_binary_means_the_default_proton(self, tmp_path, monkeypatch):
        """05-9: a whitespace-only wine_binary is "no custom Wine", so the default Proton is
        wanted. It must never reach a random plain Wine (#1338, stress round 3 u2)."""
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        patch_everywhere(
            monkeypatch, wg, "first_wine", lambda: pytest.fail("ran a random plain Wine")
        )
        backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": "   "})
        assert backend._wanted_family() is not None
        with pytest.raises(BackendError, match="Proton"):
            backend.build_launch()  # no runner installed here: Play downloads it

    def test_discovery_finds_nested_launcher(self, tmp_path):
        prefix = tmp_path / "prefix"
        exe = _install_launcher(prefix, subdir=Path("drive_c/Yostar Games/game launcher"))
        backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"})
        assert backend.find_launcher() == exe

    def test_missing_launcher_raises(self, tmp_path):
        backend = _backend(tmp_path, {"wine_binary": "/usr/bin/wine"})
        with pytest.raises(BackendError):
            backend.build_launch()


# ---------------------------------------------------------------------------
# install command construction (no network)
# ---------------------------------------------------------------------------
class TestInstall:
    def test_installer_is_downloaded_and_run_in_prefix(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
        captured: list[tuple[list[str], dict[str, str]]] = []

        def fake_stream(argv, env, on_line=None, cancel=None, before_stop=None):
            captured.append((list(argv), dict(env)))
            return 0

        patch_everywhere(monkeypatch, wg, "stream", fake_stream)
        monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)

        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
                "installer_args": ["/S", "/D=C:\\Games"],
            },
        )
        _install_launcher(prefix)
        backend.install()

        assert captured, "installer was not executed"
        argv, env = captured[0]
        assert argv == ["/usr/bin/wine", str(installer), "/S", "/D=C:\\Games"]
        assert env["WINEPREFIX"] == str(prefix)

    def test_installer_nonzero_exit_raises(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
        patch_everywhere(monkeypatch, wg, "stream", lambda *a, **k: 3)
        monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "vcredist": True,
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
            },
        )
        with pytest.raises(BackendError):
            backend.install()

    def test_missing_installer_url_raises_before_network(self, tmp_path, monkeypatch):
        def no_network(*a, **k):
            raise AssertionError("must not touch the network")

        monkeypatch.setattr(wg.download, "fetch_json", no_network)
        backend = _backend(tmp_path, {"installer_url": "https://www.arknights.global/"})
        with pytest.raises(BackendError):
            backend.install()

    def test_installer_sha256_is_passed_to_download(self, tmp_path, monkeypatch):
        """05-6: a pinned digest is enforced on the installer download."""
        digest = "b" * 64
        captured: dict = {}

        def fake_download(url, dest, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"path": Path(dest)})()

        monkeypatch.setattr(wg.download, "download", fake_download)
        monkeypatch.setattr(wg.download, "remote_size", lambda url, **kw: None)
        backend = _backend(
            tmp_path,
            {
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/a-setup.exe",
                "installer_sha256": digest,
            },
        )
        backend._download_installer(backend.require_installer_url())
        assert captured["expected_sha256"] == digest

    def test_unpinned_installer_download_guards_redirects(self, tmp_path, monkeypatch):
        """#19: without a digest, a redirect off Yostar's hosts must be refused."""
        captured: dict = {}
        sizes: dict = {}

        def fake_download(url, dest, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"path": Path(dest)})()

        def fake_size(url, **kwargs):
            sizes.update(kwargs)

        monkeypatch.setattr(wg.download, "download", fake_download)
        monkeypatch.setattr(wg.download, "remote_size", fake_size)
        backend = _backend(
            tmp_path,
            {"installer_url": "https://launcher-pkg-ark-en.yo-star.com/a-setup.exe"},
        )
        backend._download_installer(backend.require_installer_url())
        for guard in (captured["redirect_allowed"], sizes["redirect_allowed"]):
            assert guard("https://launcher-pkg-ark-en.yo-star.com/b.exe")
            assert not guard("https://evil.example/b.exe")


# ---------------------------------------------------------------------------
# verify / launch entry points
# ---------------------------------------------------------------------------
class TestVerifyAndLogin:
    def test_verify_reports_missing_pieces(self, tmp_path):
        backend = _backend(tmp_path)
        report = backend.verify()
        assert report["verified"] is False
        assert report["checks"] == {"prefix": False, "launcher": False}
        assert set(report["failed"]) == {"prefix", "launcher"}

    def test_verify_passes_with_prefix_and_launcher(self, tmp_path):
        backend = _backend(tmp_path)
        _install_launcher(backend.prefix())
        report = backend.verify()
        assert report["verified"] is True
        assert report["checks"] == {"prefix": True, "launcher": True}

    def test_login_is_unsupported(self, tmp_path):
        with pytest.raises(BackendUnsupported):
            _backend(tmp_path).login()

    def test_stellasora_install_records_the_prefix(self, tmp_path, monkeypatch):
        """N5: the Stella Sora install override must record ``installed_prefix``,
        or the profile cannot prove it made that prefix later (fails closed)."""
        from nadare.backends.stellasora import StellaSoraBackend

        outside = tmp_path / "elsewhere"
        _install_launcher(outside)
        ctx = make_context(tmp_path, StellaSoraBackend.kind, {"prefix": str(outside)})
        backend = StellaSoraBackend(ctx)
        monkeypatch.setattr(StellaSoraBackend, "_require_manifest", lambda self: "manifest")
        monkeypatch.setattr(StellaSoraBackend, "_sync_direct", lambda self: {})

        backend.install()

        assert ctx.profile["installed_prefix"] == str(outside)


# ---------------------------------------------------------------------------
# Visual C++ runtime wiring (shared nadare.core.vcredist)
# ---------------------------------------------------------------------------
_REAL_VCREDIST_ENSURE = wg.vcredist.ensure


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


def _offline(*_a, **_k):
    raise OSError("offline")


class TestArknightsVcredist:
    def test_runs_when_the_prefix_is_created(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        backend = _backend(
            tmp_path, {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "vcredist": True}
        )
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(k))

        def fake_stream(argv, env, on_line=None, cancel=None, before_stop=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        patch_everywhere(monkeypatch, wg, "stream", fake_stream)
        backend._ensure_prefix()
        assert len(calls) == 1
        assert callable(calls[0]["run_in_prefix"])

    def test_is_off_by_default(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {})
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        backend._run_vcredist(created=True)
        assert calls == []

    def test_runs_with_vcredist_true(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"vcredist": True})
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        assert calls == [1]

    def test_vcredist_false_stays_off(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"vcredist": False})
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=True)
        backend._run_vcredist(created=False)
        assert calls == []

    def test_direct_install_creates_no_prefix_without_a_runtime(self, tmp_path, monkeypatch):
        manifest = write_exe(tmp_path / "manifest.exe", "MZ")
        backend = _backend(tmp_path, {"manifest_file": str(manifest)})
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._ensure_direct_prefix()
        assert calls == []

    def test_a_failure_never_breaks_install(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
        monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
        patch_everywhere(monkeypatch, wg, "stream", lambda *a, **k: 0)
        monkeypatch.setattr("urllib.request.urlopen", _offline)
        monkeypatch.setattr(wg.vcredist, "ensure", _REAL_VCREDIST_ENSURE)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "vcredist": True,
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
            },
        )
        _install_launcher(prefix)
        events: list = []
        backend.ctx.emit = events.append
        backend.install()
        assert any(e.kind == "status" and "skipped it" in e.message for e in events)


class TestStellaSoraVcredist:
    def test_runs_when_the_prefix_is_created(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        prefix = tmp_path / "prefix"
        backend = StellaSoraBackend(
            make_context(
                tmp_path,
                StellaSoraBackend.kind,
                {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "vcredist": True},
                "stella",
            )
        )
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(k))

        def fake_stream(argv, env, on_line=None, cancel=None, before_stop=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        patch_everywhere(monkeypatch, wg, "stream", fake_stream)
        backend._ensure_prefix()
        assert len(calls) == 1

    def test_runs_with_vcredist_true_on_an_existing_prefix(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        backend = StellaSoraBackend(
            make_context(tmp_path, StellaSoraBackend.kind, {"vcredist": True}, "stella")
        )
        calls: list = []
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        assert calls == [1]

    def test_is_off_unless_the_profile_asks(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        for setting in ({}, {"vcredist": False}):
            backend = StellaSoraBackend(
                make_context(tmp_path, StellaSoraBackend.kind, setting, "stella")
            )
            calls: list = []
            monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, _c=calls, **k: _c.append(1))
            backend._run_vcredist(created=True)
            backend._run_vcredist(created=False)
            assert calls == []

