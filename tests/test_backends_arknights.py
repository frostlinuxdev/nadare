# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Arknights (Yostar) backend.

Nothing here opens a socket or starts Wine: the installer/download paths are
monkeypatched, and every path lives under the isolated ``tmp_path`` XDG tree.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core.backends import BackendError, BackendUnsupported, get, kinds


def _backend(tmp_path: Path, profile: dict | None = None, name: str = "test") -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile, name))


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
        monkeypatch.setattr(ak, "scan_pids", lambda patterns: [4242])
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
        assert backend.installer_url() == ak.DEFAULT_INSTALLER_URL
        assert ak.DEFAULT_INSTALLER_URL.endswith(".exe")

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
        monkeypatch.setattr(ak.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == url

    def test_api_url_on_untrusted_host_falls_back_to_pinned(self, tmp_path, monkeypatch):
        payload = {"data": {"rows": [{"client": "https://cdn.test/Arknights-setup.exe"}]}}
        monkeypatch.setattr(ak.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == ak.DEFAULT_INSTALLER_URL

    def test_api_failure_falls_back_to_pinned(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise ak.download.DownloadError("offline")

        monkeypatch.setattr(ak.download, "fetch_json", boom)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == ak.DEFAULT_INSTALLER_URL

    def test_api_with_html_link_is_ignored(self, tmp_path, monkeypatch):
        payload = {"data": {"rows": [{"client": "https://www.arknights.global/"}]}}
        monkeypatch.setattr(ak.download, "fetch_json", lambda *a, **k: payload)
        backend = _backend(tmp_path)
        assert backend.installer_url(resolve=True) == ak.DEFAULT_INSTALLER_URL


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
        monkeypatch.setattr(ak.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(
            ak.shutil, "which", lambda name: "/opt/umu-run" if name == "umu-run" else None
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
        monkeypatch.setattr(ak.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(ak.shutil, "which", lambda name: None)
        monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: True)
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
        monkeypatch.setattr(ak.runners, "resolve_runner", lambda value: proton)
        monkeypatch.setattr(ak.shutil, "which", lambda name: None)
        monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
        return _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"}), proton

    def test_proton_runner_without_umu_runs_proton_directly(self, tmp_path, monkeypatch):
        backend, proton = self._proton_without_umu(tmp_path, monkeypatch)
        monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: [])
        argv, env, _target = backend.build_launch()
        assert argv[:2] == [str(proton / "proton"), "run"]
        assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")

    def test_proton_without_umu_and_without_host_libs_explains_what_to_install(
        self, tmp_path, monkeypatch
    ):
        backend, _proton = self._proton_without_umu(tmp_path, monkeypatch)
        monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
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

    def test_whitespace_wine_binary_falls_back(self, tmp_path, monkeypatch):
        """05-9: a whitespace-only wine_binary must not become the name "None"."""
        prefix = tmp_path / "prefix"
        _install_launcher(prefix)
        monkeypatch.setattr(ak, "first_wine", lambda: Path("/opt/wine/bin/wine"))
        backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": "   "})
        argv, *_ = backend.build_launch()
        assert argv[0] == "/opt/wine/bin/wine"

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

        def fake_stream(argv, env, on_line=None, cancel=None):
            captured.append((list(argv), dict(env)))
            return 0

        monkeypatch.setattr(ak, "stream", fake_stream)
        monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer)

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
        monkeypatch.setattr(ak, "stream", lambda *a, **k: 3)
        monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
            },
        )
        with pytest.raises(BackendError):
            backend.install()

    def test_missing_installer_url_raises_before_network(self, tmp_path, monkeypatch):
        def no_network(*a, **k):
            raise AssertionError("must not touch the network")

        monkeypatch.setattr(ak.download, "fetch_json", no_network)
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

        monkeypatch.setattr(ak.download, "download", fake_download)
        monkeypatch.setattr(ak.download, "remote_size", lambda url, **kw: None)
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

        monkeypatch.setattr(ak.download, "download", fake_download)
        monkeypatch.setattr(ak.download, "remote_size", fake_size)
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

    def test_uninstall_removes_prefix_and_purges_data(self, tmp_path):
        backend = _backend(tmp_path)
        prefix = backend.prefix()
        _install_launcher(prefix)
        data_dir = backend._data_dir()
        assert data_dir.is_dir()
        backend.uninstall(purge=True)
        assert not prefix.exists()
        assert not data_dir.exists()

    def test_uninstall_removes_an_external_prefix(self, tmp_path):
        """A prefix outside the profile data dir that this profile recorded at
        install time (an imported/moved install) is removable."""
        outside = tmp_path / "elsewhere"
        _install_launcher(outside)
        backend = _backend(tmp_path, {"prefix": str(outside), "installed_prefix": str(outside)})

        backend.uninstall()

        assert not outside.exists()

    def test_stellasora_install_records_the_prefix(self, tmp_path, monkeypatch):
        """N5: the Stella Sora install override must record ``installed_prefix``,
        or an external prefix can never be uninstalled (fails closed)."""
        from nadare.backends.stellasora import StellaSoraBackend

        outside = tmp_path / "elsewhere"
        _install_launcher(outside)
        ctx = make_context(tmp_path, StellaSoraBackend.kind, {"prefix": str(outside)})
        backend = StellaSoraBackend(ctx)
        monkeypatch.setattr(StellaSoraBackend, "_require_manifest", lambda self: "manifest")
        monkeypatch.setattr(StellaSoraBackend, "_sync_direct", lambda self: {})

        backend.install()

        assert ctx.profile["installed_prefix"] == str(outside)
        backend.uninstall()
        assert not outside.exists()

    def test_uninstall_refuses_an_unrecorded_external_prefix(self, tmp_path):
        """A crafted/imported external prefix that was never installed by this
        profile must not be deleted."""
        outside = tmp_path / "elsewhere"
        _install_launcher(outside)
        backend = _backend(tmp_path, {"prefix": str(outside)})

        with pytest.raises(BackendError):
            backend.uninstall()

        assert outside.exists()

    def test_uninstall_refuses_another_profile_dir(self, tmp_path):
        """A prefix pointing at another Nadare profile's tree is refused."""
        other = ak.paths.profiles_dir() / "other-game"
        _install_launcher(other)
        backend = _backend(tmp_path, {"prefix": str(other), "installed_prefix": str(other)})

        with pytest.raises(BackendError):
            backend.uninstall()

        assert other.exists()

    def test_uninstall_refuses_unrecorded_symlinked_prefix(self, tmp_path):
        """A symlinked external prefix is refused when not recorded, instead of
        silently surviving an ``rmtree`` on the link (NEW-R4-3)."""
        target = tmp_path / "real-prefix"
        _install_launcher(target)
        link = tmp_path / "linked"
        link.symlink_to(target, target_is_directory=True)
        backend = _backend(tmp_path, {"prefix": str(link)})

        with pytest.raises(BackendError):
            backend.uninstall()

        assert target.exists()

    def test_uninstall_through_a_symlinked_prefix_removes_only_the_link(self, tmp_path):
        """#614: a prefix that is a link (moved to another disk, or swapped for a
        link after install) may point at a folder holding other data. Only the
        link goes; the folder it points at is left alone."""
        victim = tmp_path / "victim"
        _install_launcher(victim)
        (victim / "user-data.txt").write_text("keep")
        prefix = tmp_path / "prefix"
        prefix.symlink_to(victim, target_is_directory=True)
        backend = _backend(tmp_path, {"prefix": str(prefix), "installed_prefix": str(prefix)})

        backend.uninstall()

        assert not prefix.is_symlink()
        assert (victim / "user-data.txt").exists()

    def test_uninstall_purge_on_symlinked_profile_dir(self, tmp_path):
        """Purging a profile whose data dir is a symlink removes the tree and
        the link instead of raising ``OSError`` (NEW-R4-2)."""
        backend = _backend(tmp_path)
        data_dir = backend._data_dir()
        other_disk = tmp_path / "other-disk"
        other_disk.mkdir()
        data_dir.rmdir()
        data_dir.symlink_to(other_disk, target_is_directory=True)
        _install_launcher(backend.prefix())
        # The relocated tree is only purgeable when the install recorded it.
        backend.ctx.set("installed_prefix", str(backend.prefix()))

        (other_disk / "unrelated.txt").write_text("keep me")

        backend.uninstall(purge=True)

        # The link goes, but the folder it pointed at may hold other data.
        assert not data_dir.exists()
        assert (other_disk / "unrelated.txt").read_text() == "keep me"

    def test_purge_refuses_profile_dir_symlinked_to_an_arbitrary_directory(self, tmp_path):
        """N3: a profile dir symlinked to an arbitrary directory (here ``/opt``'s
        stand-in) must be refused, not ``rmtree``'d."""
        backend = _backend(tmp_path)
        data_dir = backend._data_dir()
        arbitrary = tmp_path / "arbitrary"
        arbitrary.mkdir()
        (arbitrary / "keep.txt").write_text("data")
        data_dir.rmdir()
        data_dir.symlink_to(arbitrary, target_is_directory=True)

        with pytest.raises(BackendError):
            backend.uninstall(purge=True)

        assert (arbitrary / "keep.txt").is_file()

    def test_purge_refuses_profile_dir_symlinked_to_another_profile(self, tmp_path):
        """A profile data dir symlinked onto another profile's tree is refused."""
        data_dir = _backend(tmp_path)._data_dir()
        other = ak.paths.profiles_dir() / "other"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("data")
        data_dir.rmdir()
        data_dir.symlink_to(other, target_is_directory=True)
        backend = _backend(tmp_path)

        with pytest.raises(BackendError):
            backend.uninstall(purge=True)

        assert (other / "keep.txt").is_file()

    def test_uninstall_refuses_catastrophic_targets(self, tmp_path):
        """Only clearly dangerous targets are refused: the filesystem root, the
        home dir, and the data dir or any of its ancestors."""
        data_dir = _backend(tmp_path)._data_dir()
        for target in (Path("/"), data_dir, data_dir.parent, Path.home()):
            backend = _backend(tmp_path, {"prefix": str(target)})
            with pytest.raises(BackendError):
                backend.uninstall()
            with pytest.raises(BackendError):
                backend.uninstall(purge=True)
        assert data_dir.is_dir()

    def test_uninstall_works_when_profile_dir_is_symlinked(self, tmp_path):
        """A profile relocated to another disk via a symlinked profile dir must
        stay usable and uninstallable."""
        backend = _backend(tmp_path)
        data_dir = backend._data_dir()
        other_disk = tmp_path / "other-disk"
        other_disk.mkdir()
        data_dir.rmdir()
        data_dir.symlink_to(other_disk, target_is_directory=True)

        prefix = backend.prefix()
        _install_launcher(prefix)
        assert (other_disk / "prefix" / ak.DEFAULT_INSTALL_SUBDIR / "game launcher.exe").is_file()

        backend.uninstall()

        assert not (other_disk / "prefix").exists()
        # The profile dir symlink itself is data, not the game tree.
        assert data_dir.is_symlink()

    def test_uninstall_purge_does_not_swallow_rmtree_errors(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path)

        def boom(path, *, ignore_errors=False):
            if ignore_errors:
                return
            raise OSError("device busy")

        monkeypatch.setattr(ak, "shutil", types.SimpleNamespace(rmtree=boom))
        # No prefix/drive_c exists, so the only rmtree is the purge of the
        # profile tree; its OSError must surface instead of being ignored.
        assert not (backend.prefix() / "drive_c").is_dir()
        with pytest.raises(OSError):
            backend.uninstall(purge=True)


# ---------------------------------------------------------------------------
# Visual C++ runtime wiring (shared nadare.core.vcredist)
# ---------------------------------------------------------------------------
_REAL_VCREDIST_ENSURE = ak.vcredist.ensure


@pytest.fixture(autouse=True)
def _no_real_vcredist(monkeypatch):
    """The VC++ step always runs now; keep it out of tests about other things."""
    monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _hermetic_runners(monkeypatch):
    """No test may read the developer's real runners or download one."""
    monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise ak.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(ak.runners, "ensure_pinned", offline)


def _offline(*_a, **_k):
    raise OSError("offline")


class TestArknightsVcredist:
    def test_runs_when_the_prefix_is_created(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"})
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(k))

        def fake_stream(argv, env, on_line=None, cancel=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        monkeypatch.setattr(ak, "stream", fake_stream)
        backend._ensure_prefix()
        assert len(calls) == 1
        assert callable(calls[0]["run_in_prefix"])

    def test_runs_on_an_existing_prefix_too(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {})
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        assert calls == [1]

    def test_runs_with_vcredist_true(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"vcredist": True})
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        assert calls == [1]

    def test_a_leftover_vcredist_false_is_ignored(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"vcredist": False})
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=True)
        backend._run_vcredist(created=False)
        assert calls == [1, 1]

    def test_direct_install_creates_no_prefix_without_a_runtime(self, tmp_path, monkeypatch):
        manifest = write_exe(tmp_path / "manifest.exe", "MZ")
        backend = _backend(tmp_path, {"manifest_file": str(manifest)})
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._ensure_direct_prefix()
        assert calls == []

    def test_a_failure_never_breaks_install(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
        monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
        monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer)
        monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)
        monkeypatch.setattr("urllib.request.urlopen", _offline)
        monkeypatch.setattr(ak.vcredist, "ensure", _REAL_VCREDIST_ENSURE)
        backend = _backend(
            tmp_path,
            {
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe",
            },
        )
        _install_launcher(prefix)
        events: list = []
        backend.ctx.emit = events.append
        backend.install()
        assert any(e.kind == "status" and "not installed" in e.message for e in events)


class TestStellaSoraVcredist:
    def test_runs_when_the_prefix_is_created(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        prefix = tmp_path / "prefix"
        backend = StellaSoraBackend(
            make_context(
                tmp_path,
                StellaSoraBackend.kind,
                {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"},
                "stella",
            )
        )
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(k))

        def fake_stream(argv, env, on_line=None, cancel=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        monkeypatch.setattr(ak, "stream", fake_stream)
        backend._ensure_prefix()
        assert len(calls) == 1

    def test_runs_with_vcredist_true_on_an_existing_prefix(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        backend = StellaSoraBackend(
            make_context(tmp_path, StellaSoraBackend.kind, {"vcredist": True}, "stella")
        )
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=False)
        assert calls == [1]

    def test_a_leftover_vcredist_false_is_ignored(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        backend = StellaSoraBackend(
            make_context(tmp_path, StellaSoraBackend.kind, {"vcredist": False}, "stella")
        )
        calls: list = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
        backend._run_vcredist(created=True)
        backend._run_vcredist(created=False)
        assert calls == [1, 1]


# ---------------------------------------------------------------------------
# display driver
# ---------------------------------------------------------------------------
class TestDisplayDriver:
    @pytest.mark.parametrize(
        ("display", "expected"),
        [(None, "1"), ("auto", "1"), ("wayland", "1"), ("x11", "0"), ("XWayland", "0")],
    )
    def test_proton_wayland_follows_profile(self, tmp_path, monkeypatch, display, expected):
        monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
        backend = _backend(tmp_path, {"display": display} if display else {})
        monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
        monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/umu-run")
        _argv, env = backend._prefix_command(["game.exe"])
        assert env["PROTON_ENABLE_WAYLAND"] == expected

    def test_runtime_note_names_runner_and_display(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"display": "x11"})
        runner = tmp_path / "dwproton-11.0-13-x86_64"
        runner.mkdir()
        (runner / "version").write_text("1789931294 dwproton-11.0-13\n")
        monkeypatch.setattr(backend, "resolve_runner", lambda: (runner, "proton"))
        assert backend._runtime_note() == "runner: dwproton-11.0-13, display: x11"


class TestDxvkConfigDefault:
    """Arknights turns off DXVK 3.1.1's present-timing path by default (#136);
    a profile ``extra_env`` still wins, and Stella Sora is left alone."""

    @staticmethod
    def _env(backend, monkeypatch, tmp_path):
        monkeypatch.delenv("DXVK_CONFIG", raising=False)
        monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
        monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/umu-run")
        return backend._prefix_command(["game.exe"])[1]

    def test_default_disables_present_timing(self, tmp_path, monkeypatch):
        env = self._env(_backend(tmp_path), monkeypatch, tmp_path)
        assert env["DXVK_CONFIG"] == "dxvk.enablePresentTiming = False"

    def test_profile_extra_env_wins(self, tmp_path, monkeypatch):
        backend = _backend(tmp_path, {"extra_env": {"DXVK_CONFIG": "dxvk.hud = fps"}})
        assert self._env(backend, monkeypatch, tmp_path)["DXVK_CONFIG"] == "dxvk.hud = fps"

    def test_stellasora_not_touched(self, tmp_path, monkeypatch):
        from nadare.backends.stellasora import StellaSoraBackend

        backend = StellaSoraBackend(make_context(tmp_path, StellaSoraBackend.kind, {}, "stella"))
        assert "DXVK_CONFIG" not in self._env(backend, monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# default runner: the pinned DW-Proton, reused when installed, never re-downloaded
# ---------------------------------------------------------------------------
class TestDefaultRunner:
    @pytest.fixture(autouse=True)
    def _no_system_wine(self, monkeypatch):
        monkeypatch.setattr(ak.shutil, "which", lambda name: None)

    def _installed(self, monkeypatch, tmp_path):
        pinned = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
        pinned.mkdir(parents=True)
        (pinned / "proton").write_text("")
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": pinned)
        return pinned

    def test_an_unset_runner_uses_the_installed_pinned_build(self, tmp_path, monkeypatch):
        pinned = self._installed(monkeypatch, tmp_path)
        backend = _backend(tmp_path, {})
        assert backend.resolve_runner() == (str(pinned), "proton")
        assert backend._runtime_available() is True

    def test_status_style_lookups_never_download(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)
        monkeypatch.setattr(ak.runners, "ensure_pinned", lambda *a, **k: pytest.fail("downloaded"))
        backend = _backend(tmp_path, {})
        assert backend.resolve_runner() == (None, "wine")
        assert backend._runtime_available() is False

    def test_install_downloads_the_pinned_build_once_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)
        calls: list = []
        monkeypatch.setattr(
            ak.runners, "ensure_pinned", lambda *a, **k: calls.append(1) or tmp_path / "x"
        )
        backend = _backend(tmp_path, {})
        backend._ensure_default_runner(strict=False)
        assert calls == [1]

    def test_an_installed_pinned_build_is_not_downloaded_again(self, tmp_path, monkeypatch):
        self._installed(monkeypatch, tmp_path)
        monkeypatch.setattr(ak.runners, "ensure_pinned", lambda *a, **k: pytest.fail("downloaded"))
        backend = _backend(tmp_path, {})
        backend._ensure_default_runner(strict=True)
        backend._ensure_default_runner(strict=False)

    def test_an_explicit_runner_or_wine_binary_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)
        monkeypatch.setattr(ak.runners, "ensure_pinned", lambda *a, **k: pytest.fail("downloaded"))
        _backend(tmp_path, {"runner": "MyProton"})._ensure_default_runner(strict=True)
        _backend(tmp_path, {"wine_binary": "/usr/bin/wine"})._ensure_default_runner(strict=True)

    @pytest.mark.parametrize("value", ["system", "wine", "none", "System"])
    def test_a_system_wine_runner_value_means_the_default_proton(
        self, tmp_path, monkeypatch, value
    ):
        pinned = self._installed(monkeypatch, tmp_path)
        backend = _backend(tmp_path, {"runner": value})
        assert backend.resolve_runner() == (str(pinned), "proton")
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)
        calls: list = []
        monkeypatch.setattr(
            ak.runners, "ensure_pinned", lambda *a, **k: calls.append(1) or tmp_path / "x"
        )
        backend._ensure_default_runner(strict=True)
        assert calls == [1]

    def test_an_existing_prefix_with_system_wine_still_gets_the_pinned_proton(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/wine")
        pinned = self._installed(monkeypatch, tmp_path)
        prefix = tmp_path / "prefix"
        (prefix / "drive_c").mkdir(parents=True)
        backend = _backend(tmp_path, {"prefix": str(prefix)})
        assert backend.resolve_runner() == (str(pinned), "proton")
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)
        calls: list = []
        monkeypatch.setattr(
            ak.runners, "ensure_pinned", lambda *a, **k: calls.append(1) or tmp_path / "x"
        )
        backend._ensure_default_runner(strict=True)
        assert calls == [1]

    def test_a_new_prefix_prefers_the_pinned_build_even_with_system_wine(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/wine")
        pinned = self._installed(monkeypatch, tmp_path)
        backend = _backend(tmp_path, {"prefix": str(tmp_path / "fresh")})
        assert backend.resolve_runner() == (str(pinned), "proton")

    def test_launch_fails_clearly_when_the_runner_cannot_be_set_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)

        def offline(*_a, **_k):
            raise ak.runners.RunnerError("offline")

        monkeypatch.setattr(ak.runners, "ensure_pinned", offline)
        backend = _backend(tmp_path, {})
        with pytest.raises(BackendError, match="Proton runner"):
            backend._ensure_default_runner(strict=True)
        events: list = []
        backend.ctx.emit = events.append
        backend._ensure_default_runner(strict=False)  # install carries on
        assert any(e.kind == "status" and "tried again" in e.message for e in events)


class TestFamilyIdsAndErrors:
    """Audit blockers: family ids must resolve outside CZN, and any download
    failure must stay a clean message (non-strict) or a BackendError (strict)."""

    @pytest.fixture(autouse=True)
    def _no_system_wine(self, monkeypatch):
        monkeypatch.setattr(ak.shutil, "which", lambda name: None)

    def _pinned(self, monkeypatch, tmp_path, family="dwproton", installed=True):
        d = tmp_path / "runners" / f"{family}-pinned-x86_64"
        d.mkdir(parents=True)
        (d / "proton").write_text("")
        monkeypatch.setattr(
            ak.runners,
            "installed_pinned",
            lambda fam="dwproton": d if installed and fam == family else None,
        )
        return d

    @pytest.mark.parametrize("family", ["dwproton", "ge", "cachyos"])
    def test_a_family_id_resolves_to_that_familys_pinned_build(self, tmp_path, monkeypatch, family):
        pinned = self._pinned(monkeypatch, tmp_path, family)
        backend = _backend(tmp_path, {"runner": family})
        assert backend.resolve_runner() == (str(pinned), "proton")
        assert backend._runtime_available() is True

    def test_a_family_id_that_is_not_installed_downloads_that_family_once(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda fam="dwproton": None)
        calls: list = []
        monkeypatch.setattr(
            ak.runners, "ensure_pinned", lambda fam="dwproton", **k: calls.append(fam) or tmp_path
        )
        backend = _backend(tmp_path, {"runner": "ge"})
        assert backend.resolve_runner() == (None, "wine")  # lookups never download
        backend._ensure_default_runner(strict=True)
        assert calls == ["ge"]

    @pytest.mark.parametrize(
        "error",
        [
            ak.download.DownloadError("network down"),
            ak.download.IntegrityError("checksum mismatch"),
            OSError("disk full"),
            BackendError("not enough free disk space"),
        ],
    )
    def test_download_failures_are_clean_not_raw(self, tmp_path, monkeypatch, error):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda fam="dwproton": None)

        def fail(*_a, **_k):
            raise error

        monkeypatch.setattr(ak.runners, "ensure_pinned", fail)
        backend = _backend(tmp_path, {})
        events: list = []
        backend.ctx.emit = events.append
        backend._ensure_default_runner(strict=False)  # install carries on
        assert any(e.kind == "status" and "tried again" in e.message for e in events)
        with pytest.raises(BackendError):
            backend._ensure_default_runner(strict=True)  # launch gets a BackendError

    def test_a_cancel_during_the_download_is_not_swallowed(self, tmp_path, monkeypatch):
        from nadare.core.events import Cancelled

        monkeypatch.setattr(ak.runners, "installed_pinned", lambda fam="dwproton": None)

        def cancelled(*_a, **_k):
            raise Cancelled()

        monkeypatch.setattr(ak.runners, "ensure_pinned", cancelled)
        backend = _backend(tmp_path, {})
        with pytest.raises(Cancelled):
            backend._ensure_default_runner(strict=False)
        with pytest.raises(Cancelled):
            backend._ensure_default_runner(strict=True)

    def test_launching_an_uninstalled_game_does_not_download_a_runner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ak.runners, "installed_pinned", lambda fam="dwproton": None)
        monkeypatch.setattr(ak.runners, "ensure_pinned", lambda *a, **k: pytest.fail("downloaded"))
        manifest = write_exe(tmp_path / "manifest.exe", "MZ")
        direct = _backend(tmp_path, {"manifest_file": str(manifest)})
        with pytest.raises(BackendError, match="not found"):
            direct.launch()
        launcher = _backend(tmp_path / "other", {})
        with pytest.raises(BackendError, match="not installed"):
            launcher.launch()
