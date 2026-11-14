# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CznBackend: status, verify, WebView2 integrity, runner choice (split out of
test_backends_czn.py, #1248)."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest
from _backend_helpers import x64_dll
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    _install_game,
    _make_backend,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _register_webview2,
    _reset_czn_process_state,
    _write_session,
    fake_keyring,
    make_backend_context,
)
from _parts_patch import patch_everywhere

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import (
    CznBackend,
)
from nadare.core.backends import BackendError


# ---------------------------------------------------------------------------
# CznBackend.status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_not_installed(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        status = backend.status()
        assert status.state == "not_installed"
        assert status.running is False
        assert status.version is None
        assert status.data["installed"] is False
        assert status.data["prefix"] == str(backend.prefix())

    def test_installed_reports_manifest_version(self, make_backend_context, tmp_path):
        prefix = tmp_path / "prefix"
        _install_game(prefix, 42100)
        backend = _make_backend(make_backend_context, prefix)
        status = backend.status()
        assert status.state == "installed"
        assert status.version == "v42100"
        assert "42100" in status.message
        assert status.data["installed"] is True

    def test_logged_in_and_session(self, make_backend_context, tmp_path, fake_keyring):
        prefix = tmp_path / "prefix"
        _install_game(prefix)
        backend = _make_backend(make_backend_context, prefix)
        backend.ctx.profile["remember_me"] = True
        backend._save_credential("refresh-token", "42")
        _write_session(backend._session().parent, valid=True)

        status = backend.status()
        assert status.data["logged_in"] is True
        assert status.data["session"]["valid"] is True

    def test_expired_session_is_reported_invalid(self, make_backend_context, tmp_path):
        prefix = tmp_path / "prefix"
        _install_game(prefix)
        backend = _make_backend(make_backend_context, prefix)
        _write_session(backend._session().parent, valid=False)
        status = backend.status()
        assert status.data["session"]["valid"] is False

    def test_missing_session_is_none(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        assert backend.status().data["session"] is None


# ---------------------------------------------------------------------------
# CznBackend.verify
# ---------------------------------------------------------------------------


class TestVerify:
    def _full_install(self, backend: CznBackend) -> None:
        prefix = backend.prefix()
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        _install_game(prefix)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ")
        webview = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0.0"
        webview.mkdir(parents=True, exist_ok=True)
        (webview / "msedgewebview2.exe").write_bytes(b"MZ")
        _register_webview2(prefix)
        system32 = prefix / "drive_c/windows/system32"
        system32.mkdir(parents=True, exist_ok=True)
        for dll in ("vcruntime140.dll", "msvcp140.dll"):
            (system32 / dll).write_bytes(x64_dll(b"native"))
        _write_session(backend._session().parent, valid=True)

    def test_all_present(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        self._full_install(backend)
        report = backend.verify()
        assert report["verified"] is True
        assert all(report["checks"].values())
        assert report["prefix"] == str(backend.prefix())
        assert report["failed"] == []
        assert report["message"] == "all checks passed"

    def test_missing_pieces(self, make_backend_context, tmp_path):
        prefix = tmp_path / "prefix"
        _install_game(prefix)  # no drive_c, stub, session or webview2
        backend = _make_backend(make_backend_context, prefix)
        report = backend.verify()
        assert report["verified"] is False
        assert report["checks"]["game"] is True
        assert report["checks"]["stub"] is False
        # The message must name the failing checks, or the CLI/GUI says nothing.
        assert "stub" in report["failed"]
        assert "stub" in report["message"]


# ---------------------------------------------------------------------------
# CznBackend._ensure_webview2 integrity (pinned SHA-256, Microsoft hosts)
# ---------------------------------------------------------------------------
class TestWebView2Integrity:
    def _backend(self, make_backend_context, tmp_path) -> CznBackend:
        return _make_backend(make_backend_context, tmp_path / "prefix")

    @pytest.fixture(autouse=True)
    def _no_pin_env(self, monkeypatch):
        """The pin may also come from the environment; keep tests hermetic."""
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)

    def test_refuses_when_the_hash_is_unpinned(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)
        with pytest.raises(BackendError):
            backend._verify_webview2(inst)

    def test_rejects_a_mismatched_installer_and_removes_it(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        with pytest.raises(BackendError):
            backend._verify_webview2(inst)
        assert not inst.exists()

    def test_accepts_a_matching_installer(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(
            monkeypatch, czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest()
        )
        backend._verify_webview2(inst)  # must not raise
        assert inst.exists()

    def test_a_cached_installer_is_reverified_before_it_runs(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir()
        inst = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        inst.write_bytes(b"tampered")
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        ran: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_in_prefix", lambda args: ran.append(args) or 0)
        with pytest.raises(BackendError):
            backend._ensure_webview2()
        assert ran == []

    def test_poisoned_cache_entry_is_evicted_for_a_recoverable_retry(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """N3: a cached installer that fails the pin is removed, so the next
        attempt re-downloads instead of failing forever."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir()
        inst = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        inst.write_bytes(b"tampered")
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)

        with pytest.raises(BackendError) as excinfo:
            backend._ensure_webview2()

        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_FAILED
        assert not inst.exists()

    def test_install_fails_fast_without_a_pin_and_never_downloads(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """NEW-1: an unpinned install must refuse before any network activity."""
        backend = self._backend(make_backend_context, tmp_path)
        # Pretend the prefix already exists so the fail-fast is what stops us.
        (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
        monkeypatch.setattr(backend, "_game_running", lambda: False)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)

        def _no_network(*args, **kwargs):
            raise AssertionError("install() tried to download the installer")

        monkeypatch.setattr("urllib.request.urlopen", _no_network)
        with pytest.raises(BackendError) as excinfo:
            backend.install()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_UNVERIFIED
        assert "webview2" in excinfo.value.message.lower()
        assert "NADARE_WEBVIEW2_SHA256" not in excinfo.value.message
        assert "Install again" in excinfo.value.message

    def test_env_pin_is_honoured(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)
        monkeypatch.setenv("NADARE_WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest())
        backend._verify_webview2(inst)  # must not raise
        assert inst.exists()

    def test_profile_pin_is_honoured(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        backend.ctx.profile["webview2_sha256"] = hashlib.sha256(b"MZ").hexdigest()
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)
        backend._verify_webview2(inst)  # must not raise
        assert inst.exists()

    def test_cached_installer_read_error_is_mapped_to_a_backenderror(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """NEW-2: an unreadable cached installer must surface as a BackendError."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir()
        inst = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)

        def _unreadable(_path):
            raise OSError("EIO")

        monkeypatch.setattr("nadare.core.webview2._sha256_file", _unreadable)
        with pytest.raises(BackendError) as excinfo:
            backend._ensure_webview2()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_FAILED

    def test_whitespace_only_pin_falls_through(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """FRESH-1: a blank env pin must not mask a lower-precedence pin."""
        backend = self._backend(make_backend_context, tmp_path)
        backend.ctx.profile["webview2_sha256"] = "b" * 64
        monkeypatch.setenv("NADARE_WEBVIEW2_SHA256", "   ")
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)
        assert backend._webview2_pin() == "b" * 64

    def test_malformed_pin_fails_fast_without_download(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """FRESH-3: a malformed pin must be refused before the ~215 MB download."""
        backend = self._backend(make_backend_context, tmp_path)
        (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
        monkeypatch.setattr(backend, "_game_running", lambda: False)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "deadbeef")

        def _no_network(*args, **kwargs):
            raise AssertionError("downloaded despite a malformed pin")

        monkeypatch.setattr("urllib.request.urlopen", _no_network)
        with pytest.raises(BackendError) as excinfo:
            backend.install()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_UNVERIFIED
        assert "malformed" in excinfo.value.message.lower()

    def test_cache_dir_error_is_mapped_to_a_backenderror(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """FRESH-2: a cache mkdir/stat OSError must not escape raw."""
        backend = self._backend(make_backend_context, tmp_path)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)

        def _boom():
            raise OSError("EACCES")

        monkeypatch.setattr(backend, "_cache_dir", _boom)
        with pytest.raises(BackendError) as excinfo:
            backend._ensure_webview2()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_FAILED

    def test_executes_a_private_read_only_copy(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """FRESH-6: the verified installer is run from a read-only private copy."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir()
        inst = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        inst.write_bytes(b"MZ")
        patch_everywhere(
            monkeypatch, czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest()
        )
        state = {"installed": False}
        monkeypatch.setattr(backend, "_webview2_installed", lambda: state["installed"])
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        seen: dict[str, object] = {}

        def fake_run(args):
            path = Path(args[0])
            seen["path"] = path
            seen["mode"] = stat.S_IMODE(path.stat().st_mode)
            state["installed"] = True
            return 0

        monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
        backend._ensure_webview2()

        assert seen["path"] != inst
        assert seen["mode"] == 0o400
        assert not seen["path"].exists()

    def test_download_branch_refuses_an_off_microsoft_redirect(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """N8: the download path must refuse a redirect that leaves Microsoft."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        patch_everywhere(
            monkeypatch, czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest()
        )

        class _Response:
            headers = {"Content-Length": "2"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return "https://evil.example/installer.exe"

            def read(self, _size=-1):
                return b"MZ"

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: _Response())
        with pytest.raises(BackendError) as excinfo:
            backend._ensure_webview2()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_FAILED
        assert "Microsoft" in excinfo.value.message
        part = (cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").with_suffix(".part")
        assert not part.exists()

    def test_download_branch_never_runs_an_installer_that_fails_the_pin(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """N8: bytes that do not match the pin must never reach the prefix."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        patch_everywhere(
            monkeypatch, czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"other").hexdigest()
        )
        ran: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_in_prefix", lambda args: ran.append(args) or 0)

        class _Response:
            headers = {"Content-Length": "2"}
            _done = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return "https://go.microsoft.com/fwlink/?linkid=2124701"

            def read(self, _size=-1):
                if self._done:
                    return b""
                self._done = True
                return b"MZ"

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: _Response())
        with pytest.raises(BackendError) as excinfo:
            backend._ensure_webview2()
        assert excinfo.value.code == czn_backend.EXIT_WEBVIEW2_FAILED
        assert ran == []

    def test_download_branch_installs_a_matching_installer(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """N8: the full download -> verify -> run path works with a good pin."""
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        payload = b"MZ"
        patch_everywhere(
            monkeypatch, czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(payload).hexdigest()
        )
        state = {"installed": False}
        monkeypatch.setattr(backend, "_webview2_installed", lambda: state["installed"])
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        ran: list[list[str]] = []

        class _Response:
            headers = {"Content-Length": str(len(payload))}
            _done = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return "https://go.microsoft.com/fwlink/?linkid=2124701"

            def read(self, _size=-1):
                if self._done:
                    return b""
                self._done = True
                return payload

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: _Response())

        def fake_run(args):
            ran.append(args)
            state["installed"] = True
            return 0

        monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
        backend._ensure_webview2()
        assert (cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").is_file()
        assert ran and ran[0][1:] == ["/silent", "/install"]

    def test_backend_webview2_codes_are_not_in_the_subprocess_table(self):
        """FRESH-7: backend codes 10/11 must not shadow czn-linux exit codes."""
        assert czn_backend.EXIT_WEBVIEW2_UNVERIFIED not in czn_backend.EXIT_MESSAGES
        assert czn_backend.EXIT_WEBVIEW2_FAILED not in czn_backend.EXIT_MESSAGES
        assert czn_backend.WEBVIEW2_MESSAGES[czn_backend.EXIT_WEBVIEW2_UNVERIFIED]
        assert czn_backend.WEBVIEW2_MESSAGES[czn_backend.EXIT_WEBVIEW2_FAILED]


# ---------------------------------------------------------------------------
# CznBackend.resolve_runner
# ---------------------------------------------------------------------------
class TestResolveRunner:
    def test_wine_word_is_the_default_runner(self, make_backend_context, tmp_path, monkeypatch):
        # Not plain system Wine any more (#1310 W9-1): see test_czn_runner_wine_words.py.
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        monkeypatch.setattr(backend, "_pinned_runner", lambda rid: tmp_path / rid)
        backend.ctx.profile["runner"] = "wine"
        assert backend.resolve_runner() == (str(tmp_path / "dwproton"), "proton")

    def test_explicit_proton_dir(self, make_backend_context, tmp_path):
        proton = tmp_path / "MyProton"
        proton.mkdir()
        (proton / "proton").write_text("#!/bin/sh\n")
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = str(proton)
        assert backend.resolve_runner() == (str(proton), "proton")

    def test_runner_id_found_in_runners_dir(self, make_backend_context, tmp_path, monkeypatch):
        root = tmp_path / "runners"
        found = root / "Proton-CachyOS-cachyos-11.0"
        found.mkdir(parents=True)
        (found / "proton").write_text("#!/bin/sh\n")
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)

        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "cachyos"
        # Not the tested pin, and the pin cannot be downloaded: the installed build stands in.
        assert backend.resolve_runner() == (str(found), "proton")

    def test_unknown_runner_raises(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "not-a-runner"
        with pytest.raises(BackendError):
            backend.resolve_runner()

    @pytest.fixture(autouse=True)
    def _hermetic_runner_dirs(self, monkeypatch):
        """Never read the developer's real Steam/Heroic runner folders."""
        from nadare.core import runners as core_runners

        monkeypatch.setattr(core_runners, "steam_runners", lambda: [])

    def _runner_dir(self, root, name):
        d = root / name
        d.mkdir(parents=True)
        (d / "proton").write_text("#!/bin/sh\n")
        return d

    def test_tested_runner_auto_downloads_the_pinned_release(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "cachyos"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: tmp_path / "runners")
        found = tmp_path / "dl/proton-cachyos-11.0-20260703-slr-x86_64"
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            backend, "_download_runner", lambda rid, latest: calls.append((rid, latest)) or found
        )
        assert backend.resolve_runner() == (str(found), "proton")
        assert calls == [("cachyos", False)]  # a tested runner is never --latest

    def test_dwproton_downloads_the_pinned_tested_release(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: tmp_path / "runners")
        found = tmp_path / "dl/dwproton-11.0-17-x86_64"
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            backend, "_download_runner", lambda rid, latest: calls.append((rid, latest)) or found
        )
        assert backend.resolve_runner() == (str(found), "proton")
        assert calls == [("dwproton", False)]  # the tested pin, never --latest

    def test_default_runner_is_the_pinned_dwproton(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile.pop("runner", None)
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        pinned = self._runner_dir(root, "dwproton-11.0-17-x86_64")
        monkeypatch.setattr(backend, "_download_runner", lambda *a, **k: pytest.fail("downloaded"))
        assert backend.resolve_runner() == (str(pinned), "proton")

    def test_an_installed_pinned_runner_is_never_downloaded_again(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        pinned = self._runner_dir(root, "dwproton-11.0-17-x86_64")
        monkeypatch.setattr(backend, "_download_runner", lambda *a, **k: pytest.fail("downloaded"))
        for _ in range(3):
            assert backend.resolve_runner() == (str(pinned), "proton")

    def test_a_newer_untested_build_does_not_replace_the_pin(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        self._runner_dir(root, "dwproton-11.0-13-x86_64")
        pinned = self._runner_dir(root, "dwproton-11.0-17-x86_64")
        self._runner_dir(root, "dwproton-11.0-18-x86_64")
        monkeypatch.setattr(backend, "_download_runner", lambda *a, **k: pytest.fail("downloaded"))
        assert backend.resolve_runner() == (str(pinned), "proton")

    def test_only_untested_builds_installed_downloads_the_pin(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        self._runner_dir(root, "dwproton-11.0-18-x86_64")
        got = tmp_path / "dl/dwproton-11.0-17-x86_64"
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            backend, "_download_runner", lambda rid, latest: calls.append((rid, latest)) or got
        )
        assert backend.resolve_runner() == (str(got), "proton")
        assert calls == [("dwproton", False)]

    def test_offline_falls_back_to_an_installed_untested_build_with_a_notice(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        other = self._runner_dir(root, "dwproton-11.0-18-x86_64")

        def offline(*_a, **_k):
            raise BackendError("offline")

        monkeypatch.setattr(backend, "_download_runner", offline)
        events: list = []
        backend.ctx.emit = events.append
        assert backend.resolve_runner() == (str(other), "proton")
        assert any(e.kind == "status" and "untested" in e.message for e in events)

    def test_offline_with_nothing_installed_still_fails(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: tmp_path / "runners")

        def offline(*_a, **_k):
            raise BackendError("offline")

        monkeypatch.setattr(backend, "_download_runner", offline)
        with pytest.raises(BackendError):
            backend.resolve_runner()

    def test_dwproton_uses_an_already_installed_copy(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        found = self._runner_dir(root, "dwproton-11.0-17-x86_64")
        assert backend.resolve_runner() == (str(found), "proton")
