# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Chaos Zero Nightmare backend, updater and runners.

Every test works against fake directories under ``tmp_path``.  Nothing here
opens a socket or runs a subprocess that reaches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import (
    GAME_SUBDIR,
    SERVICE,
    CznBackend,
)
from nadare.core import paths
from nadare.core import secrets as core_secrets
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled, contains_value

_REAL_INDEX_PINS = CznBackend._webview2_index_pins
_REAL_INDEX_SOURCE = CznBackend._webview2_index_source


@pytest.fixture
def make_backend_context(tmp_path):
    """Local alias so the tests can build a context with a one-line call."""

    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


@pytest.fixture(autouse=True)
def _no_runner_downloads(monkeypatch):
    """No CZN test may download a runner (resolving the default one would)."""

    def offline(self, rid, latest):
        raise BackendError("offline (no runner downloads in tests)")

    monkeypatch.setattr(CznBackend, "_download_runner", offline)


@pytest.fixture(autouse=True)
def _no_webview2_index(monkeypatch):
    """Never hit the network for the published WebView2 hashes in unit tests."""
    monkeypatch.setattr(CznBackend, "_webview2_index_pins", staticmethod(lambda: set()))
    monkeypatch.setattr(CznBackend, "_webview2_index_source", staticmethod(lambda: ""))


@pytest.fixture(autouse=True)
def _reset_czn_process_state():
    """Keep the process-wide live session/keyring cache from leaking tests."""
    czn_backend._reset_process_state()
    yield
    czn_backend._reset_process_state()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_backend(make_backend_context, prefix: Path) -> CznBackend:
    ctx = make_backend_context("stove-czn", {"prefix": str(prefix)})
    return CznBackend(ctx)


def _install_game(prefix: Path, version: int = 42100) -> None:
    game = prefix / GAME_SUBDIR
    (game / "bin").mkdir(parents=True, exist_ok=True)
    (game / "bin" / "ssr-stove-shield.exe").write_bytes(b"shield")
    manifest = game / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"local_version": version}))


def _write_session(root: Path, *, valid: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    session = root / "session.json"
    expire = int(time.time() * 1000) + (3600_000 if valid else -3600_000)
    session.write_text(json.dumps({"access_token": "tok", "expire_time": str(expire)}))
    return session


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
        system32 = prefix / "drive_c/windows/system32"
        system32.mkdir(parents=True, exist_ok=True)
        for dll in ("vcruntime140.dll", "msvcp140.dll"):
            (system32 / dll).write_bytes(b"MZ-native")
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)
        with pytest.raises(BackendError):
            backend._verify_webview2(inst)

    def test_rejects_a_mismatched_installer_and_removes_it(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        with pytest.raises(BackendError):
            backend._verify_webview2(inst)
        assert not inst.exists()

    def test_accepts_a_matching_installer(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest())
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)

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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)

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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)
        monkeypatch.setenv("NADARE_WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest())
        backend._verify_webview2(inst)  # must not raise
        assert inst.exists()

    def test_profile_pin_is_honoured(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        backend.ctx.profile["webview2_sha256"] = hashlib.sha256(b"MZ").hexdigest()
        inst = tmp_path / "installer.exe"
        inst.write_bytes(b"MZ")
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)

        def _unreadable(_path):
            raise OSError("EIO")

        monkeypatch.setattr(czn_backend, "_sha256_file", _unreadable)
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)
        assert backend._webview2_pin() == "b" * 64

    def test_malformed_pin_fails_fast_without_download(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """FRESH-3: a malformed pin must be refused before the ~215 MB download."""
        backend = self._backend(make_backend_context, tmp_path)
        (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
        monkeypatch.setattr(backend, "_game_running", lambda: False)
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "deadbeef")

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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest())
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"MZ").hexdigest())

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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(b"other").hexdigest())
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
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", hashlib.sha256(payload).hexdigest())
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
    def test_wine_sentinel(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "wine"
        assert backend.resolve_runner() == ("", "wine")

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
        found = tmp_path / "dl/dwproton-11.0-15-x86_64"
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
        pinned = self._runner_dir(root, "dwproton-11.0-15-x86_64")
        monkeypatch.setattr(backend, "_download_runner", lambda *a, **k: pytest.fail("downloaded"))
        assert backend.resolve_runner() == (str(pinned), "proton")

    def test_an_installed_pinned_runner_is_never_downloaded_again(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        pinned = self._runner_dir(root, "dwproton-11.0-15-x86_64")
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
        pinned = self._runner_dir(root, "dwproton-11.0-15-x86_64")
        self._runner_dir(root, "dwproton-11.0-16-x86_64")
        monkeypatch.setattr(backend, "_download_runner", lambda *a, **k: pytest.fail("downloaded"))
        assert backend.resolve_runner() == (str(pinned), "proton")

    def test_only_untested_builds_installed_downloads_the_pin(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        self._runner_dir(root, "dwproton-11.0-16-x86_64")
        got = tmp_path / "dl/dwproton-11.0-15-x86_64"
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
        other = self._runner_dir(root, "dwproton-11.0-16-x86_64")

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
        found = self._runner_dir(root, "dwproton-11.0-15-x86_64")
        assert backend.resolve_runner() == (str(found), "proton")


# ---------------------------------------------------------------------------
# czn.update — pure helpers
# ---------------------------------------------------------------------------
def _manifest_files(*rows: str) -> dict:
    return {"version_no": 10, "root_folder": "root", "execution": "game.exe", "files": list(rows)}


def _write_matching(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.md5(data).hexdigest()


class TestUpdateHelpers:
    def test_parse_files(self):
        manifest = _manifest_files(
            "0|D|/somedir",
            "1|F|/a.bin|3|chunk0|gz|11|22|" + "a" * 32 + "|" + "b" * 32 + "|x",
            "bad",
        )
        rows = list(czn_update.parse_files(manifest))
        assert rows[0] == ("D", "/somedir", None, None, None, None, None)
        kind, path, vf, ci, md5u, md5p, usize = rows[1]
        assert (kind, path, vf, ci, usize) == ("F", "/a.bin", "3", "chunk0", 11)
        assert (md5u, md5p) == ("a" * 32, "b" * 32)
        assert len(rows) == 2  # "bad" row skipped

    def test_plan_matches_and_missing(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        data = b"hello"
        md5 = _write_matching(game_dir / "a.bin", data)
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x",
            "1|F|/missing.bin|3|c1|g|5|9|" + "e" * 32 + "|" + "f" * 32 + "|x",
            "2|D|/newdir",
            "3|F|/vcredist/x.bin|3|c|g|1|1|" + "0" * 32 + "|" + "0" * 32 + "|x",
        )
        to_download, dirs = czn_update.plan(manifest)
        assert [p for p, *_ in to_download] == ["/missing.bin"]
        assert dirs == ["/newdir"]

    def test_plan_rejects_traversal_file(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        manifest = _manifest_files(
            "0|F|/../evil.bin|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
        )
        with pytest.raises(czn_update.ManifestError):
            czn_update.plan(manifest)
        assert not (tmp_path / "evil.bin").exists()

    def test_plan_rejects_traversal_dir(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        with pytest.raises(czn_update.ManifestError):
            czn_update.plan(_manifest_files("0|D|/../evil"))
        assert not (tmp_path / "evil").exists()

    def test_plan_rejects_absolute_and_drive_paths(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        for bad in ("C:/evil.bin", "//evil.bin", r"\evil.bin"):
            manifest = _manifest_files(
                "0|F|" + bad + "|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            )
            with pytest.raises(czn_update.ManifestError):
                czn_update.plan(manifest)

    def test_local_version(self, monkeypatch, tmp_path):
        upf = tmp_path / "GameManifest.upf"
        upf.write_text(json.dumps({"local_version": 42100}))
        monkeypatch.setattr(czn_update, "UPF", upf)
        assert czn_update.local_version() == 42100

    def test_local_version_missing_is_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "nope.upf")
        assert czn_update.local_version() == 0

    def test_game_running_false_for_absent_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "nope")
        assert czn_update.game_running() is False

    def test_parse_files_rejects_a_malformed_size(self):
        """FRESH-4: a non-numeric usize is a ManifestError, not a ValueError."""
        manifest = _manifest_files(
            "0|F|/a.bin|3|c0|g|abc|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
        )
        with pytest.raises(czn_update.ManifestError):
            list(czn_update.parse_files(manifest))

    def test_cmdline_match_ignores_an_incidental_substring(self):
        """L2: the path only counts at an argv-token boundary."""
        prefix = "z:\\home\\game\\bin\\"
        assert not czn_update._cmdline_has_exe(
            b"editor\0/home/user/notes-z:\\home\\game\\bin\\x", prefix
        )
        assert czn_update._cmdline_has_exe(
            b"Z:\\home\\game\\bin\\shield.exe\0--launch", prefix
        )


class TestResolveLatest:
    def test_index_at_installed_cannot_suppress_updates(self, monkeypatch):
        """NEW-3: an unsigned index must not pin latest at the installed version."""
        monkeypatch.setattr(czn_update, "indexed_version", lambda: 10)
        monkeypatch.setattr(czn_update, "_head_ok", lambda url: True)
        probed: dict[str, int] = {}

        def fake_latest(installed):
            probed["installed"] = installed
            return 12

        monkeypatch.setattr(czn_update, "latest_version", fake_latest)
        assert czn_update.resolve_latest(10) == (12, "probe")
        assert probed == {"installed": 10}

    def test_index_strictly_newer_than_installed_is_used(self, monkeypatch):
        monkeypatch.setattr(czn_update, "indexed_version", lambda: 11)
        monkeypatch.setattr(czn_update, "_head_ok", lambda url: True)

        def _boom(installed):
            raise AssertionError("must not probe when the index is newer")

        monkeypatch.setattr(czn_update, "latest_version", _boom)
        assert czn_update.resolve_latest(10) == (11, "index")


class TestUpdateMain:
    def test_check_without_game_dir_is_offline(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "missing")
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        code = czn_update.main(["check"])
        assert code == 5
        assert "game dir not found" in capsys.readouterr().out

    def test_dry_run_up_to_date(self, monkeypatch, tmp_path, capsys):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: _manifest_files("0|D|/adir"))
        code = czn_update.main(["check"])
        out = capsys.readouterr().out
        assert code == 0
        assert "up to date" in out

    def test_apply_up_to_date_still_bumps_the_version_marker(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        upf = tmp_path / "upf.json"
        upf.write_text(json.dumps({"local_version": 55, "game_id": "STOVE_CHAOSZERO"}))
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "UPF", upf)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (56, "index"))
        manifest = _manifest_files("0|D|/adir")
        manifest["version_no"] = 56
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: manifest)
        assert czn_update.main(["check"]) == 0
        assert json.loads(upf.read_text())["local_version"] == 55  # dry run writes nothing
        assert czn_update.main(["update", "--apply"]) == 0
        assert json.loads(upf.read_text())["local_version"] == 56

    def test_apply_checks_disk_space_for_total(self, monkeypatch, tmp_path, capsys):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: _manifest_files("0|D|/adir"))
        monkeypatch.setattr(
            czn_update,
            "plan",
            lambda manifest: ([
                ("/a.bin", "3", "c0", "u" * 32, "p" * 32, 1234),
                ("/b.bin", "3", "c0", "u" * 32, "p" * 32, 66),
            ], []),
        )
        monkeypatch.setattr(czn_update, "game_running", lambda: False)
        monkeypatch.setattr(czn_update, "download_file", lambda *a, **k: 1)
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "upf.json")
        calls: list[tuple[Path, int]] = []
        monkeypatch.setattr(
            czn_update.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )
        assert czn_update.main(["update", "--apply"]) == 0
        assert calls == [(game_dir, 1300)]

    def test_download_file_writes_bytes(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "_fetch_verified", lambda *a, **k: b"payload")
        n = czn_update.download_file("/sub/file.bin", "3", "c0", "u", "p")
        assert n == len(b"payload")
        assert (game_dir / "sub/file.bin").read_bytes() == b"payload"

    def test_apply_rejects_a_traversal_manifest_and_writes_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "upf.json")
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(
            czn_update,
            "fetch_manifest",
            lambda v: _manifest_files(
                "0|F|/../evil.bin|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            ),
        )
        assert czn_update.main(["update", "--apply"]) == 2
        assert "rejected" in capsys.readouterr().out
        assert not (tmp_path / "evil.bin").exists()

    def test_malformed_manifest_size_is_reported_without_traceback(
        self, monkeypatch, tmp_path, capsys
    ):
        """FRESH-4: a bad usize surfaces through the documented error path."""
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(
            czn_update,
            "fetch_manifest",
            lambda v: _manifest_files(
                "0|F|/a.bin|3|c0|g|abc|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            ),
        )
        assert czn_update.main(["update", "--apply"]) == 2
        assert "rejected" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# update.indexed_version (public index parsing)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status


class _FakeSession:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._resp = _FakeResp(body, status)

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._resp


def test_indexed_version_accepts_matching_service(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"service": "STOVE_CHAOSZERO", "latest": 77}).encode()
    monkeypatch.setenv("CZN_MANIFEST_INDEX", "https://example.test/latest.json")
    monkeypatch.setattr(czn_update, "_SESS", _FakeSession(body))
    assert czn_update.indexed_version() == 77


def test_indexed_version_rejects_wrong_service_or_non_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"service": "OTHER", "latest": 77}).encode()
    monkeypatch.setenv("CZN_MANIFEST_INDEX", "https://example.test/latest.json")
    monkeypatch.setattr(czn_update, "_SESS", _FakeSession(body))
    assert czn_update.indexed_version() is None

    monkeypatch.setenv("CZN_MANIFEST_INDEX", "")
    assert czn_update.indexed_version() is None

# ---------------------------------------------------------------------------
# CZN credential hardening (no plaintext, keyring only, ephemeral session)
# ---------------------------------------------------------------------------

SENTINEL = "SENTINEL-REFRESH-TOKEN-8f3a2b7c"


class _FakeKeyring:
    """Minimal stand-in for the ``keyring`` module (no disk, no service)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    kr = _FakeKeyring()
    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: kr)
    return kr


def _cred_backend(make_backend_context, tmp_path, remember: bool = False) -> CznBackend:
    ctx = make_backend_context(
        "stove-czn", {"remember_me": remember, "prefix": str(tmp_path / "prefix")}
    )
    return CznBackend(ctx)


def _credential_blob(fake_keyring: _FakeKeyring) -> dict:
    assert len(fake_keyring.store) == 1, fake_keyring.store
    return json.loads(next(iter(fake_keyring.store.values())))


def _auth_runner(
    calls: list[list[str]],
    credential: dict | None = None,
    code: int = 0,
    *,
    write_session: bool = True,
):
    """Build a fake ``_run_module`` that mimics the auth worker's handoff.

    The credential is delivered through the 0600 file named by
    ``CZN_CREDENTIAL_OUT`` -- never through stdout/events.
    """

    def run(module, args, **kwargs):
        calls.append(list(args))
        extra = kwargs.get("extra_env") or {}
        handoff = extra.get("CZN_CREDENTIAL_OUT")
        if handoff and credential is not None:
            path = Path(handoff)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(credential))
        if write_session and "--out" in args:
            out = Path(args[args.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{}")
            os.chmod(out, 0o600)
        return code

    return run


class TestCredentialStorage:
    def test_remember_off_persists_nothing(self, make_backend_context, tmp_path, fake_keyring):
        backend = _cred_backend(make_backend_context, tmp_path, remember=False)
        backend._save_credential(SENTINEL, "42")

        assert fake_keyring.store == {}
        assert backend._refresh_token == SENTINEL
        assert backend._load_credential() is None
        assert not (paths.config_dir() / "secrets.json").exists()

    def test_failed_save_drops_the_superseded_keyring_entry(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential("OLD-TOKEN", "42")
        assert len(fake_keyring.store) == 1

        def refuse(service, account, value):
            raise RuntimeError("keyring locked")

        monkeypatch.setattr(fake_keyring, "set_password", refuse)
        backend._save_credential("ROTATED-TOKEN", "42")

        assert fake_keyring.store == {}
        assert backend._load_credential() is None
        assert backend._refresh_token == "ROTATED-TOKEN"

    def test_remember_on_blob_has_only_refresh_and_member(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")

        blob = _credential_blob(fake_keyring)
        assert blob == {"refresh_token": SENTINEL, "member_no": "42"}
        raw = next(iter(fake_keyring.store.values()))
        assert "access_token" not in raw
        assert "game_token" not in raw
        assert not (paths.config_dir() / "secrets.json").exists()

    def test_turning_remember_off_deletes_keyring_entry(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        assert fake_keyring.store

        backend.set_remember(False)
        assert fake_keyring.store == {}
        assert backend.ctx.profile["remember_me"] is False
        # The live in-memory session survives; only persistence is gone.
        assert backend._refresh_token == SENTINEL

    def test_set_remember_true_persists_the_live_session(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        # A login happened with remember off (live session in memory only).
        a = _cred_backend(make_backend_context, tmp_path, remember=False)
        a._save_credential(SENTINEL, "42")
        assert fake_keyring.store == {}

        # A brand-new backend object adopts the process-wide live session; a
        # later Remember me toggle must write the keyring blob with no mint.
        b = _cred_backend(make_backend_context, tmp_path, remember=False)
        calls: list[list[str]] = []
        monkeypatch.setattr(b, "_run_module", lambda *args, **kwargs: calls.append(args) or 1)

        b.set_remember(True)

        assert calls == []
        assert b.ctx.profile["remember_me"] is True
        assert _credential_blob(fake_keyring) == {
            "refresh_token": SENTINEL,
            "member_no": "42",
        }

    def test_set_remember_persist_flag_is_opt_out(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=False)

        backend.set_remember(True, persist_flag=False)
        assert backend.ctx.profile["remember_me"] is False

        backend.set_remember(True)
        assert backend.ctx.profile["remember_me"] is True

    def test_missing_keyring_keeps_session_in_memory_and_notices(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(core_secrets, "_keyring_module", lambda: None)
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        events = []
        backend.ctx.emit = events.append

        backend._save_credential(SENTINEL, "42")

        assert backend._refresh_token == SENTINEL
        assert any(
            e.kind == "status" and "Remember me unavailable" in e.message for e in events
        ), events
        assert not (paths.config_dir() / "secrets.json").exists()


class TestProcessWideSession:
    """The GUI builds a fresh backend object per operation (gui/workers.py),
    so the live session must live in a process-wide store, not on the object."""

    def test_login_survives_a_new_backend_object(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        a = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(
            a,
            "_run_module",
            _auth_runner([], {"refresh_token": SENTINEL, "member_no": "42"}),
        )
        a.login()  # remember_me is off; only process memory may carry the token
        assert czn_backend._live_get("test")["refresh_token"] == SENTINEL

        b = _cred_backend(make_backend_context, tmp_path)
        assert b._refresh_token == SENTINEL
        TestEphemeralSession()._launchable(b, tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            b,
            "_run_module",
            _auth_runner(calls, {"refresh_token": SENTINEL, "member_no": "42"}),
        )
        monkeypatch.setattr(b, "_stream", lambda *args, **kwargs: 0)

        assert b.launch() == 0
        assert any("token" in c for c in calls), calls

    def test_launch_without_any_session_asks_to_log_in(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], None))

        with pytest.raises(BackendError) as exc:
            backend.launch()
        assert exc.value.code == 3
        assert "log in first, or enable remember_me" in exc.value.message

    def test_set_remember_off_keeps_the_live_session(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")

        backend.set_remember(False)

        assert czn_backend._live_get("test")["refresh_token"] == SENTINEL

    def test_forget_saved_credential_clears_keyring_but_keeps_live_session(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        assert fake_keyring.store
        assert czn_backend._keyring_credential("test") is not None

        czn_backend.forget_saved_credential("test")

        assert fake_keyring.store == {}
        assert czn_backend._keyring_credential("test") is None
        # The live in-memory session is deliberately left alone.
        assert czn_backend._live_get("test")["refresh_token"] == SENTINEL

    def test_logout_clears_keyring_and_memory(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")

        backend.logout()

        assert backend._refresh_token is None
        assert czn_backend._live_get("test") is None
        assert fake_keyring.store == {}

    def test_logout_surfaces_a_keyring_delete_failure(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """N7: a failed keyring delete is reported while local state is cleared."""
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        assert fake_keyring.store

        def _locked(service, account):
            raise RuntimeError("keyring is locked")

        monkeypatch.setattr(fake_keyring, "delete_password", _locked)
        events = []
        backend.ctx.emit = events.append

        backend.logout()

        assert backend._refresh_token is None
        assert czn_backend._live_get("test") is None
        assert czn_backend._keyring_credential("test") is None
        assert any(e.kind == "status" and "keyring" in e.message.lower() for e in events), events
        assert events[-1].message == "Logged out of STOVE"


class TestStatusKeyringCache:
    def test_status_reads_the_keyring_once_per_process(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        seeded = _cred_backend(make_backend_context, tmp_path, remember=True)
        seeded._save_credential(SENTINEL, "42")
        reads: list[str] = []
        orig = fake_keyring.get_password
        monkeypatch.setattr(
            fake_keyring,
            "get_password",
            lambda service, account: (reads.append(account), orig(service, account))[1],
        )
        # Simulate a fresh process: drop live/cached state but keep the keyring.
        czn_backend._reset_process_state()

        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        assert backend.status().data["logged_in"] is True
        assert backend.status().data["logged_in"] is True
        assert len(reads) == 1, reads

    def test_transient_keyring_failure_is_not_cached(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """M3: a locked keyring is retried, not remembered as 'logged out'."""
        seeded = _cred_backend(make_backend_context, tmp_path, remember=True)
        seeded._save_credential(SENTINEL, "42")
        czn_backend._reset_process_state()
        calls = {"n": 0}
        orig = core_secrets.SecretStore.get

        def flaky(self, profile):
            calls["n"] += 1
            if calls["n"] == 1:
                raise core_secrets.SecretStoreError("locked")
            return orig(self, profile)

        monkeypatch.setattr(core_secrets.SecretStore, "get", flaky)
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)

        assert backend.status().data["logged_in"] is False
        assert backend.status().data["logged_in"] is True
        assert calls["n"] == 2


class TestLegacyStateMigration:
    def _write_legacy(self, backend: CznBackend, **fields) -> Path:
        auth_dir = backend._auth_dir()
        auth_dir.mkdir(parents=True, exist_ok=True)
        state = {"caller_detail": "a" * 40, "guid": "guid-1", "member_no": "42"}
        state.update(fields)
        path = auth_dir / "czn_auth.json"
        path.write_text(json.dumps(state))
        return path

    def test_moves_refresh_to_keyring_when_remember_on(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        path = self._write_legacy(backend, launcher_refresh=SENTINEL)

        backend._migrate_legacy_state()

        blob = _credential_blob(fake_keyring)
        assert blob == {"refresh_token": SENTINEL, "member_no": "42"}
        rewritten = json.loads(path.read_text())
        assert "launcher_refresh" not in rewritten
        assert rewritten["caller_detail"] == "a" * 40

    def test_drops_refresh_when_remember_off(
        self, make_backend_context, tmp_path, fake_keyring
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=False)
        path = self._write_legacy(backend, launcher_refresh=SENTINEL)

        backend._migrate_legacy_state()

        assert fake_keyring.store == {}
        rewritten = json.loads(path.read_text())
        assert "launcher_refresh" not in rewritten
        assert rewritten["guid"] == "guid-1"

    def test_migration_is_idempotent(self, make_backend_context, tmp_path, fake_keyring):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        self._write_legacy(backend, launcher_refresh=SENTINEL)
        backend._migrate_legacy_state()
        backend._migrate_legacy_state()
        assert _credential_blob(fake_keyring)["refresh_token"] == SENTINEL
        assert len(fake_keyring.store) == 1


class TestRestore:
    def _seed(self, backend: CznBackend, member: str = "42", token: str = SENTINEL) -> None:
        backend.ctx.profile["remember_me"] = True
        backend._save_credential(token, member)

    def test_restore_refreshes_once_and_persists_rotation(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        self._seed(backend)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner(calls, {"refresh_token": "rotated-token", "member_no": "42"}),
        )

        assert backend.restore() is True
        assert backend.restore() is True
        assert len(calls) == 1
        assert _credential_blob(fake_keyring)["refresh_token"] == "rotated-token"

    def test_member_mismatch_deletes_credential_and_requires_login(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        self._seed(backend, member="42")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner(calls, {"refresh_token": "rotated", "member_no": "99"}),
        )
        events = []
        backend.ctx.emit = events.append

        assert backend.restore() is False
        assert fake_keyring.store == {}
        assert len(calls) == 1
        assert any("log in again" in e.message.lower() for e in events), events

    def test_network_error_logs_out_without_retry(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        self._seed(backend)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _auth_runner(calls, None, code=4))

        assert backend.restore() is False
        assert backend.restore() is False
        assert len(calls) == 1
        # Logged out in memory, but the keyring entry is kept for another start.
        assert backend._refresh_token is None
        assert fake_keyring.store

    def test_rejected_refresh_token_is_forgotten_everywhere(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """A 44010 (auth worker exit 3) means the stored chain is dead: drop the
        keyring entry and the in-process cache, so a fresh backend does not
        adopt it and ``status`` stops claiming a login (#73)."""
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        self._seed(backend)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _auth_runner(calls, None, code=3))

        assert backend.restore() is False
        assert fake_keyring.store == {}
        assert czn_backend._live_get(backend.ctx.name) is None
        assert backend._refresh_token is None
        assert backend._member_no is None

        fresh = _cred_backend(make_backend_context, tmp_path, remember=True)
        assert fresh._refresh_token is None
        assert fresh.restore() is False
        assert len(calls) == 1  # no second network round trip with a dead token

    def test_restore_without_remember_makes_no_network_call(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=False)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _auth_runner(calls))
        assert backend.restore() is False
        assert calls == []


class TestEphemeralSession:
    def test_session_lives_next_to_the_stub(self, make_backend_context, tmp_path):
        # The umu/Steam-Runtime sandbox can read the dir the stub exe runs from;
        # this is the proven czn-linux layout (tools/session.json).
        backend = _cred_backend(make_backend_context, tmp_path)
        assert backend._session() == backend._stub().parent / "session.json"

    def test_session_is_not_in_the_sandbox_invisible_runtime_dir(
        self, make_backend_context, tmp_path
    ):
        # Regression guard: $XDG_RUNTIME_DIR is not mounted into the sandbox, so
        # a session there is invisible to the game (handshake-rejected live).
        backend = _cred_backend(make_backend_context, tmp_path)
        runtime = Path(os.environ["XDG_RUNTIME_DIR"]).resolve()
        assert not backend._session().resolve().is_relative_to(runtime)

    def test_handoff_lives_in_the_private_runtime_dir(self, make_backend_context, tmp_path):
        # The credential handoff is host-only (read by the backend), so it can
        # stay in the private per-profile runtime dir.
        backend = _cred_backend(make_backend_context, tmp_path)
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        assert backend._handoff_path() == runtime / "nadare" / "test" / "credential.json"

    def test_run_auth_removes_a_leftover_handoff_tmp(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """``credential.json.tmp`` holds the rotating refresh token; a crash
        between write and replace must not leave it behind (#93)."""
        backend = _cred_backend(make_backend_context, tmp_path)
        handoff = backend._handoff_path()
        tmp = handoff.with_name(handoff.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text('{"refresh_token": "leaked"}')
        monkeypatch.setattr(backend, "_run_module", lambda *a, **k: 0)

        backend._run_auth(["token", "--out", str(backend._session())])

        assert not tmp.exists()
        assert not handoff.exists()

    def test_run_auth_removes_the_handoff_tmp_written_by_a_crashing_worker(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        handoff = backend._handoff_path()
        tmp = handoff.with_name(handoff.name + ".tmp")

        def crash(*args, **kwargs):
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text('{"refresh_token": "leaked"}')
            raise KeyboardInterrupt

        monkeypatch.setattr(backend, "_run_module", crash)
        with pytest.raises(KeyboardInterrupt):
            backend._run_auth(["token", "--out", str(backend._session())])
        assert not tmp.exists()

    @pytest.mark.parametrize(
        ("a", "b"), [("a b", "a_b"), ("x/y", "x_y"), ("é", "_"), ("a b", "a-b")]
    )
    def test_distinct_profile_names_never_share_a_handoff_dir(self, a, b):
        """Two valid, distinct names must not slug to the same runtime dir (#72)."""
        assert czn_backend._profile_slug(a) != czn_backend._profile_slug(b)

    def test_profile_slug_is_stable_and_safe(self):
        slug = czn_backend._profile_slug
        assert slug("test") == "test"  # plain names keep their readable path
        assert slug("a_b") == "a_b"
        assert slug("a b") == slug("a b")
        for name in ("..", ".", ".hidden", "a/../b", "", "con\x00fig"):
            out = slug(name)
            assert out and "/" not in out and out not in (".", "..")
            assert not out.startswith(".")

    def test_private_write_is_0600(self, tmp_path):
        from nadare.backends.czn import auth

        path = tmp_path / "session.json"
        auth._write_private(path, "{}")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_cleanup_removes_stale_and_legacy_sessions(self, make_backend_context, tmp_path):
        backend = _cred_backend(make_backend_context, tmp_path)
        current = backend._session()
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("{}")
        legacy_data = backend._data_dir() / "session.json"
        legacy_data.write_text("{}")
        legacy_runtime = backend._session_dir() / "session.json"
        legacy_runtime.write_text("{}")

        backend._cleanup_stale_session()

        assert not current.exists()
        assert not legacy_data.exists()
        assert not legacy_runtime.exists()

    def test_cleanup_never_deletes_a_live_run(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        a = _cred_backend(make_backend_context, tmp_path)
        session = a._session()
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text("{}")

        monkeypatch.setattr(CznBackend, "_game_running", lambda self: True)
        b = _cred_backend(make_backend_context, tmp_path)  # construction must not delete
        assert session.exists()
        b._cleanup_stale_session()  # an explicit sweep must also refuse
        assert session.exists()

    def test_unlink_quietly_removes_the_file_and_tolerates_missing(self, tmp_path):
        path = tmp_path / "session.json"
        path.write_text("{}")
        czn_backend._unlink_quietly(path)
        assert not path.exists()
        czn_backend._unlink_quietly(path)  # missing must not raise

    def test_launch_registers_session_for_process_exit_cleanup(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        self._launchable(backend, tmp_path, monkeypatch)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": SENTINEL, "member_no": "42"}),
        )
        monkeypatch.setattr(backend, "_stream", lambda *args, **kwargs: 0)
        registered: list[tuple] = []
        monkeypatch.setattr(
            czn_backend.atexit,
            "register",
            lambda fn, *args, **kwargs: registered.append((fn, args)),
        )

        backend.launch()

        assert any(args == (backend._session(),) for _fn, args in registered), registered

    def test_launch_registers_session_only_once_per_path(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        monkeypatch.setattr(czn_backend, "_registered_ephemeral", set())
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        self._launchable(backend, tmp_path, monkeypatch)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": SENTINEL, "member_no": "42"}),
        )
        monkeypatch.setattr(backend, "_stream", lambda *args, **kwargs: 0)
        registered: list[tuple] = []
        monkeypatch.setattr(
            czn_backend.atexit,
            "register",
            lambda fn, *args, **kwargs: registered.append((fn, args)),
        )

        backend.launch()
        backend.launch()

        session = backend._session()
        assert [args for _fn, args in registered] == [(session,)]

    def _launchable(self, backend: CznBackend, tmp_path, monkeypatch) -> None:
        prefix = backend.prefix()
        _install_game(prefix)
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ")
        monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
        monkeypatch.setattr(backend, "_prefix_command", lambda args, extra=None: (["stub"], {}))
        monkeypatch.setattr(backend, "_stub_result", lambda log: "ok")

    def test_launch_writes_session_right_before_launch_and_deletes_it(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        self._launchable(backend, tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner(calls, {"refresh_token": "rotated", "member_no": "42"}),
        )
        observed: dict[str, object] = {}

        def fake_stream(cmd, env, **kwargs):
            session = backend._session()
            observed["exists"] = session.exists()
            observed["mode"] = stat.S_IMODE(session.stat().st_mode)
            return 0

        monkeypatch.setattr(backend, "_stream", fake_stream)

        assert backend.launch() == 0
        assert observed == {"exists": True, "mode": 0o600}
        assert not backend._session().exists()
        # Restore minted the session once right before launch.
        assert len(calls) == 1

    def test_launch_deletes_session_even_when_launch_fails(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        self._launchable(backend, tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner(calls, {"refresh_token": "rotated", "member_no": "42"}),
        )

        def boom(cmd, env, **kwargs):
            raise RuntimeError("stub exploded")

        monkeypatch.setattr(backend, "_stream", boom)

        with pytest.raises(RuntimeError):
            backend.launch()
        assert not backend._session().exists()


class TestQrStatePolish:
    def test_qr_status_maps_to_gui_states(self, make_backend_context, tmp_path):
        backend = _cred_backend(make_backend_context, tmp_path)
        events = []
        backend.ctx.emit = events.append

        for state, expected in (
            ("init", "waiting"),
            ("ongoing", "waiting"),
            ("success", "approved"),
            ("expired", "expired"),
            ("close", "expired"),
            ("anything-else", "waiting"),
        ):
            backend._emit_json_event({"event": "qr_status", "state": state})
            event = events[-1]
            assert event.kind == "step"
            assert event.data["qr_state"] == expected
            assert event.data["state"] == state


class TestNoTokenInEvents:
    def test_credential_event_is_never_emitted(self, make_backend_context, tmp_path):
        backend = _cred_backend(make_backend_context, tmp_path)
        events = []
        backend.ctx.emit = events.append

        backend._emit_json_event(
            {"event": "credential", "refresh_token": SENTINEL, "member_no": "42"}
        )
        backend._emit_json_event(
            {"event": "session", "path": "/tmp/session.json", "expires_at": 1, "expires_in": 2}
        )
        backend._emit_json_event({"event": "qr_status", "state": "ongoing"})

        assert events, "expected some events to be emitted"
        assert not any(contains_value(e, SENTINEL) for e in events)

    def test_full_restore_launch_never_emits_token(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner(calls, {"refresh_token": SENTINEL, "member_no": "42"}),
        )
        monkeypatch.setattr(backend, "_stream", lambda cmd, env, **kwargs: 0)
        events = []
        backend.ctx.emit = events.append

        assert backend.restore() is True
        assert backend.launch() == 0
        assert events
        assert not any(contains_value(e, SENTINEL) for e in events)


# ---------------------------------------------------------------------------
# login lifecycle (M1/M2) and auth-worker hygiene (M4/L1)
# ---------------------------------------------------------------------------
class TestLoginLifecycle:
    def test_login_deletes_the_minted_game_session(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """M1: the game token minted by login must not persist on disk."""
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": SENTINEL, "member_no": "42"}),
        )

        backend.login()

        assert not backend._session().exists()
        assert czn_backend._live_get("test")["refresh_token"] == SENTINEL

    def test_login_keeps_a_rotated_credential_when_a_later_step_fails(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        """M2: a rotation already handed off must survive a later failure."""
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": "rotated", "member_no": "42"}, code=4),
        )

        with pytest.raises(BackendError) as excinfo:
            backend.login()

        assert excinfo.value.code == 4
        assert czn_backend._live_get("test")["refresh_token"] == "rotated"
        assert not backend._session().exists()


class TestAuthWorker:
    def test_write_private_creates_parent_dirs(self, tmp_path):
        """M2: auth must be able to write --out before the stub dir exists."""
        from nadare.backends.czn import auth

        path = tmp_path / "nested" / "deeper" / "session.json"
        auth._write_private(path, "{}")
        assert path.read_text() == "{}"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_parse_does_not_leak_the_response_body(self):
        """M4: a non-JSON error must not echo the raw body."""
        from nadare.backends.czn import auth

        class _Resp:
            status_code = 502
            headers = {"Content-Type": "text/html"}
            text = "access_token=SUPERSECRETVALUE"

            def json(self):
                raise ValueError("not json")

        with pytest.raises(RuntimeError) as excinfo:
            auth.StoveAuth._parse(None, _Resp(), "gc_check")
        message = str(excinfo.value)
        assert "502" in message
        assert "text/html" in message
        assert "SUPERSECRETVALUE" not in message

    def test_default_qr_png_is_outside_the_package_tree(self):
        """L1: the standalone QR default must not land in site-packages."""
        from nadare.backends.czn import auth

        png = auth._default_qr_png()
        assert png.parent == auth.STATE_DIR
        assert Path(auth.__file__).parent not in png.resolve().parents


class TestGameProcessMatch:
    def test_cmdline_match_ignores_an_incidental_substring(self):
        """L2: the game path only counts as a whole argv token."""
        prefix = "z:\\game\\bin\\"
        assert not czn_backend._cmdline_has_exe(
            b"sh\0-c\0echo z:\\game\\bin\\x", prefix
        )
        assert czn_backend._cmdline_has_exe(b"Z:\\game\\bin\\game.exe", prefix)


class TestPrebuiltStub:
    def _setup(self, monkeypatch, tmp_path, data: bytes, digest: str):
        src = tmp_path / "assets" / "czn_pcsdk_stub.exe"
        src.parent.mkdir()
        src.write_bytes(data)
        monkeypatch.setattr(czn_backend, "STUB_PREBUILT", src)
        monkeypatch.setattr(czn_backend, "STUB_PREBUILT_SHA256", digest)

    def test_copies_verified_prebuilt(self, monkeypatch, tmp_path):
        import hashlib

        data = b"MZ-stub"
        self._setup(monkeypatch, tmp_path, data, hashlib.sha256(data).hexdigest())
        dest = tmp_path / "out" / "stub" / "czn_pcsdk_stub.exe"
        assert CznBackend._install_prebuilt_stub(dest) is True
        assert dest.read_bytes() == data
        assert not dest.with_name(dest.name + ".part").exists()

    def test_rejects_digest_mismatch(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path, b"tampered", "0" * 64)
        dest = tmp_path / "out" / "czn_pcsdk_stub.exe"
        assert CznBackend._install_prebuilt_stub(dest) is False
        assert not dest.exists()

    def test_missing_prebuilt_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(czn_backend, "STUB_PREBUILT", tmp_path / "nope.exe")
        assert CznBackend._install_prebuilt_stub(tmp_path / "x.exe") is False

    def test_bundled_asset_matches_pinned_digest(self):
        import hashlib

        data = czn_backend.STUB_PREBUILT.read_bytes()
        assert hashlib.sha256(data).hexdigest() == czn_backend.STUB_PREBUILT_SHA256


class TestProtonWithoutUmu:
    def _backend(self, make_backend_context, tmp_path, monkeypatch):
        proton = tmp_path / "MyProton"
        proton.mkdir()
        (proton / "proton").write_text("#!/bin/sh\n")
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = str(proton)
        return backend, proton

    def test_direct_proton_when_umu_missing(self, make_backend_context, tmp_path, monkeypatch):
        backend, proton = self._backend(make_backend_context, tmp_path, monkeypatch)
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        argv, env = backend._prefix_command(["game.exe"])
        assert argv == [str(proton / "proton"), "run", "game.exe"]
        assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")

    def test_umu_preferred_when_present(self, make_backend_context, tmp_path, monkeypatch):
        backend, proton = self._backend(make_backend_context, tmp_path, monkeypatch)
        monkeypatch.setattr(
            czn_backend.shutil, "which", lambda name, *a, **k: "/usr/bin/umu-run"
        )
        argv, env = backend._prefix_command(["game.exe"])
        assert argv == ["/usr/bin/umu-run", "game.exe"]
        assert env["PROTONPATH"] == str(proton)


class TestGlThreading:
    def _backend(self, make_backend_context, tmp_path):
        proton = tmp_path / "MyProton"
        proton.mkdir()
        (proton / "proton").write_text("#!/bin/sh\n")
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = str(proton)
        return backend

    def test_off_by_default(self, make_backend_context, tmp_path, monkeypatch):
        monkeypatch.delenv("mesa_glthread", raising=False)
        monkeypatch.delenv("__GL_THREADED_OPTIMIZATIONS", raising=False)
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        backend = self._backend(make_backend_context, tmp_path)
        _argv, env = backend._prefix_command(["game.exe"])
        assert "mesa_glthread" not in env and "__GL_THREADED_OPTIMIZATIONS" not in env

    def test_profile_opt_in_and_extra_env_wins(self, make_backend_context, tmp_path, monkeypatch):
        monkeypatch.delenv("mesa_glthread", raising=False)
        monkeypatch.delenv("__GL_THREADED_OPTIMIZATIONS", raising=False)
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        backend = self._backend(make_backend_context, tmp_path)
        backend.ctx.profile["gl_threading"] = True
        _argv, env = backend._prefix_command(["game.exe"])
        assert env["mesa_glthread"] == "true"
        assert env["__GL_THREADED_OPTIMIZATIONS"] == "1"
        _argv, env = backend._prefix_command(["game.exe"], extra_env={"mesa_glthread": "false"})
        assert env["mesa_glthread"] == "false"


class TestWebView2IndexPins:
    def _inst(self, tmp_path, data=b"MZ-new"):
        inst = tmp_path / "inst.exe"
        inst.write_bytes(data)
        return inst

    def test_index_hash_accepted_when_builtin_pin_is_stale(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        import hashlib

        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        data = b"MZ-new"
        monkeypatch.setattr(
            CznBackend,
            "_webview2_index_pins",
            staticmethod(lambda: {hashlib.sha256(data).hexdigest()}),
        )
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        inst = self._inst(tmp_path, data)
        backend._verify_webview2(inst)  # must not raise
        assert inst.exists()

    def test_unknown_hash_still_refused(self, make_backend_context, tmp_path, monkeypatch):
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(
            CznBackend, "_webview2_index_pins", staticmethod(lambda: {"1" * 64})
        )
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        inst = self._inst(tmp_path)
        with pytest.raises(BackendError):
            backend._verify_webview2(inst)
        assert not inst.exists()

    def test_explicit_override_ignores_index(self, make_backend_context, tmp_path, monkeypatch):
        import hashlib

        data = b"MZ-new"
        monkeypatch.setenv("NADARE_WEBVIEW2_SHA256", "2" * 64)
        monkeypatch.setattr(
            CznBackend,
            "_webview2_index_pins",
            staticmethod(lambda: {hashlib.sha256(data).hexdigest()}),
        )
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        with pytest.raises(BackendError):
            backend._verify_webview2(self._inst(tmp_path, data))

    def test_index_fetch_parses_and_filters(self, monkeypatch):
        import io
        import urllib.request

        body = json.dumps({"sha256": ["A" * 64, "bad", 5, "b" * 64]}).encode()

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            czn_backend.hosting, "webview2_index_url", lambda: "https://example.test/w.json"
        )
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R(body))
        assert _REAL_INDEX_PINS() == {"a" * 64, "b" * 64}

    def test_index_fetch_failure_is_empty(self, monkeypatch):
        import urllib.request

        def boom(*a, **k):
            raise OSError("offline")

        monkeypatch.setattr(
            czn_backend.hosting, "webview2_index_url", lambda: "https://example.test/w.json"
        )
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert _REAL_INDEX_PINS() == set()

    def test_non_https_index_is_ignored(self, monkeypatch):
        monkeypatch.setattr(
            czn_backend.hosting, "webview2_index_url", lambda: "http://example.test/w.json"
        )
        assert _REAL_INDEX_PINS() == set()

    def test_index_source_requires_https_microsoft(self, monkeypatch):
        import io
        import urllib.request

        def serve(url):
            body = json.dumps({"url": url}).encode()

            class R(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R(body))

        monkeypatch.setattr(
            czn_backend.hosting, "webview2_index_url", lambda: "https://example.test/w.json"
        )
        good = "https://msedge.sf.dl.delivery.mp.microsoft.com/files/g/Installer.exe"
        serve(good)
        assert _REAL_INDEX_SOURCE() == good
        serve("https://evil.example/Installer.exe")
        assert _REAL_INDEX_SOURCE() == ""
        serve("http://msedge.sf.dl.delivery.mp.microsoft.com/files/g/Installer.exe")
        assert _REAL_INDEX_SOURCE() == ""


class TestWebView2IndexedDownload:
    """The index's immutable per-build URL is tried first, fwlink is the fallback."""

    GOOD = b"MZ-build-7"
    INDEXED = "https://msedge.sf.dl.delivery.mp.microsoft.com/files/g7/Installer.exe"

    def _setup(self, make_backend_context, tmp_path, monkeypatch, served):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        cache = tmp_path / "cache"
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        # Built-in pin is stale; the index vouches for the GOOD build only.
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        digest = hashlib.sha256(self.GOOD).hexdigest()
        monkeypatch.setattr(
            CznBackend, "_webview2_index_pins", staticmethod(lambda: {digest})
        )
        monkeypatch.setattr(
            CznBackend, "_webview2_index_source", staticmethod(lambda: self.INDEXED)
        )
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        state = {"installed": False}
        monkeypatch.setattr(backend, "_webview2_installed", lambda: state["installed"])

        def run(args):
            state["installed"] = True
            return 0

        monkeypatch.setattr(backend, "_run_in_prefix", run)
        requested: list[str] = []

        class _Resp:
            def __init__(self, url, body):
                self._url, self._body, self._done = url, body, False
                self.headers = {"Content-Length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def geturl(self):
                return self._url

            def read(self, _n=-1):
                if self._done:
                    return b""
                self._done = True
                return self._body

        def fake_urlopen(url, timeout=60):
            requested.append(url)
            body = served(url)
            if body is None:
                raise OSError("404")
            return _Resp(url, body)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return backend, requested

    def test_indexed_url_used_when_fwlink_already_rotated(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        # fwlink now serves a newer build that the index has not recorded yet.
        served = lambda url: self.GOOD if url == self.INDEXED else b"MZ-newer"  # noqa: E731
        backend, requested = self._setup(make_backend_context, tmp_path, monkeypatch, served)
        backend._ensure_webview2()
        assert requested == [self.INDEXED]

    def test_falls_back_to_fwlink_when_indexed_url_is_gone(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        served = lambda url: None if url == self.INDEXED else self.GOOD  # noqa: E731
        backend, requested = self._setup(make_backend_context, tmp_path, monkeypatch, served)
        backend._ensure_webview2()
        assert requested == [self.INDEXED, czn_backend.WEBVIEW2_URL]

    def test_nothing_runs_when_both_sources_fail_verification(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        served = lambda url: b"MZ-tampered"  # noqa: E731
        backend, requested = self._setup(make_backend_context, tmp_path, monkeypatch, served)
        with pytest.raises(BackendError):
            backend._ensure_webview2()
        assert requested == [self.INDEXED, czn_backend.WEBVIEW2_URL]
        assert not backend._webview2_installed()


class TestCancelAndOverlayOptions:
    def test_cancel_interrupts_a_silent_child(self, make_backend_context, tmp_path, monkeypatch):
        """A running game prints nothing; Cancel must still stop it promptly."""
        import threading
        import time

        from nadare.core.events import Cancelled

        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        stopped: list[bool] = []
        monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
        threading.Timer(0.5, backend.ctx.cancel.cancel).start()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            backend._stream(
                ["sleep", "30"], {"PATH": os.environ.get("PATH", "")}, kill_game=True
            )
        assert time.monotonic() - started < 5
        assert stopped == [True], "cancel must also stop the game processes"

    def test_stream_still_delivers_lines_and_exit_code(
        self, make_backend_context, tmp_path
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        seen: list[str] = []
        backend.ctx.emit = lambda ev: seen.append(ev.message)  # type: ignore[assignment]
        code = backend._stream(
            ["bash", "-c", "echo one; echo two; exit 3"], {"PATH": os.environ.get("PATH", "")}
        )
        assert code == 3
        assert "one" in seen and "two" in seen

    def test_display_option_selects_wayland_default(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        assert backend._wayland_default() == "1"
        backend.ctx.profile["display"] = "x11"
        assert backend._wayland_default() == "0"

    def test_mangohud_wrap(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        env: dict[str, str] = {}
        assert backend._mangohud_wrap(["g"], env) == ["g"]
        # A legacy profile toggle must not keep MangoHud on after upgrade.
        backend.ctx.profile["mangohud"] = True
        monkeypatch.setattr(czn_backend.shutil, "which", lambda n, *a, **k: "/usr/bin/mangohud")
        assert backend._mangohud_wrap(["g"], env) == ["g"]
        assert "MANGOHUD" not in env
        # The environment override still works.
        monkeypatch.setenv("NADARE_MANGOHUD", "1")
        assert backend._mangohud_wrap(["g"], env) == ["/usr/bin/mangohud", "--dlsym", "g"]
        assert env["MANGOHUD"] == "1" and "fps" in env["MANGOHUD_CONFIG"]
        monkeypatch.setattr(czn_backend.shutil, "which", lambda n, *a, **k: None)
        assert backend._mangohud_wrap(["g"], {}) == ["g"]


class TestPlainWinePromptSuppression:
    def test_mono_and_gecko_prompts_are_disabled(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "wine"
        monkeypatch.setenv("CZN_WINE", "/usr/bin/wine")
        monkeypatch.setenv("WINEDLLOVERRIDES", "winemenubuilder.exe=d")
        argv, env = backend._prefix_command(["game.exe"])
        assert argv == ["/usr/bin/wine", "game.exe"]
        overrides = env["WINEDLLOVERRIDES"].split(";")
        assert "mscoree,mshtml=" in overrides
        assert "winemenubuilder.exe=d" in overrides, "an existing override must survive"
        # idempotent
        _argv, env2 = backend._prefix_command(["game.exe"])
        assert env2["WINEDLLOVERRIDES"].count("mscoree,mshtml=") == 1


class TestStreamDoesNotWaitForInheritedPipes:
    def test_returns_when_the_child_exits_even_if_a_daemon_holds_the_pipe(
        self, make_backend_context, tmp_path
    ):
        """wineserver-style daemons inherit stdout; the call must still return."""
        import time

        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        seen: list[str] = []
        backend.ctx.emit = lambda ev: seen.append(ev.message)  # type: ignore[assignment]
        script = "(sleep 20 &) ; echo started; exit 0"
        started = time.monotonic()
        code = backend._stream(["bash", "-c", script], {"PATH": os.environ.get("PATH", "")})
        assert code == 0
        assert time.monotonic() - started < 5
        assert "started" in seen


class TestCrashHandlerIsNotTheGame:
    PREFIX = "z:\\mnt\\x\\games\\czn\\bin\\"

    def test_helper_alone_is_not_running(self):
        raw = b"Z:\\mnt\\x\\games\\czn\\bin\\crashpad_handler.exe\0--no-rate-limit\0"
        assert czn_backend._cmdline_has_exe(raw, self.PREFIX) is True
        assert (
            czn_backend._cmdline_has_exe(raw, self.PREFIX, exclude=czn_backend.GAME_HELPER_EXES)
            is False
        )

    def test_the_shield_still_counts(self):
        raw = b"Z:\\mnt\\x\\games\\czn\\bin\\ssr-stove-shield.exe\0"
        assert czn_backend._cmdline_has_exe(
            raw, self.PREFIX, exclude=czn_backend.GAME_HELPER_EXES
        )


# ---------------------------------------------------------------------------
# File-level verify (cached manifest, offline)
# ---------------------------------------------------------------------------
class TestVerifyFiles:
    @staticmethod
    def _row(path: str, data: bytes) -> str:
        import hashlib

        md5 = hashlib.md5(data).hexdigest()
        return f"|F|{path}|1|1|0|{len(data)}|0|{md5}|{md5}"

    def test_cached_manifest_reports_missing_and_corrupt(self, tmp_path, monkeypatch, capsys):
        game = tmp_path / "game"
        (game / "bin").mkdir(parents=True)
        (game / "bin" / "ok.dll").write_bytes(b"good")
        (game / "bin" / "bad.dll").write_bytes(b"BAD!")
        manifest = {
            "version_no": 55,
            "files": [
                self._row("/bin/ok.dll", b"good"),
                self._row("/bin/bad.dll", b"good"),
                self._row("/bin/gone.dll", b"x"),
            ],
        }
        cache = game / "nadare-manifest.json"
        monkeypatch.setattr(czn_update, "GAME_DIR", game)
        monkeypatch.setattr(czn_update, "MANIFEST_CACHE", cache)
        assert czn_update.main(["verify"]) == 0
        assert json.loads(capsys.readouterr().out)["checked"] is False  # no cache yet
        cache.write_text(json.dumps(manifest))
        assert czn_update.main(["verify"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["checked"] is True
        assert sorted(out["differ"]) == ["/bin/bad.dll", "/bin/gone.dll"]

    def test_backend_verify_names_the_files(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        TestVerify()._full_install(backend)
        cache = backend.game_dir() / "combinedata_manifest" / f"nadare-manifest-{SERVICE}.json"
        cache.write_text("{}")

        def fake_run(module, args, *, parse_json=False, collect=None, extra_env=None):
            collect.append(
                {"event": "verify", "checked": True, "count": 1, "differ": ["/bin/a.dll"]}
            )
            return 0

        monkeypatch.setattr(backend, "_run_module", fake_run)
        report = backend.verify()
        assert report["verified"] is False
        assert report["failed"] == ["files"]
        assert report["bad_files"] == ["/bin/a.dll"]
        assert "/bin/a.dll" in report["message"]


# ---------------------------------------------------------------------------
# Audit fixes: per-worker exit-code tables, launch minting, keyring failure
# surfacing, credential-handoff cleanup, WebView2 cancellation and verify.
# ---------------------------------------------------------------------------
class TestExitCodeMapping:
    def test_login_reports_an_auth_failure_not_a_network_error(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], None, code=2))
        with pytest.raises(BackendError) as exc:
            backend.login()
        assert exc.value.code == 2
        assert "cannot reach the update server" not in exc.value.message
        assert "login" in exc.value.message.lower()

    def test_login_maps_exit_4_to_the_network_message(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], None, code=4))
        with pytest.raises(BackendError) as exc:
            backend.login()
        assert exc.value.code == 4
        assert exc.value.message == czn_backend.AUTH_EXIT_MESSAGES[4]

    def test_launch_token_refresh_uses_the_auth_table(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        backend._restore_attempted = True
        backend._refresh_token = SENTINEL
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], None, code=2))
        with pytest.raises(BackendError) as exc:
            backend.launch()
        assert exc.value.code == 2
        assert exc.value.message == czn_backend.AUTH_EXIT_MESSAGES[2]

    def test_update_maps_a_download_failure_not_a_usage_error(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        monkeypatch.setattr(backend, "_run_module", lambda *a, **k: 1)
        with pytest.raises(BackendError) as exc:
            backend.update()
        assert exc.value.code == 1
        assert "setup or usage error" not in exc.value.message
        assert exc.value.message == czn_backend.UPDATE_EXIT_MESSAGES[1]

    def test_install_maps_a_network_failure(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        monkeypatch.setattr(backend, "_game_running", lambda: False)
        monkeypatch.setattr(backend, "_ensure_prefix", lambda: None)
        monkeypatch.setattr(backend, "_ensure_webview2", lambda: None)
        monkeypatch.setattr(backend, "_ensure_stub", lambda: tmp_path / "stub")
        monkeypatch.setattr(backend, "_run_module", lambda *a, **k: 2)
        with pytest.raises(BackendError) as exc:
            backend.install()
        assert exc.value.code == 2
        assert exc.value.message == "cannot reach the update server"


class TestLaunchMinting:
    def test_member_mismatch_removes_the_minted_session(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": "rotated", "member_no": "99"}),
        )

        assert backend.restore() is False
        assert not backend._session().exists()

    def test_second_launch_mints_a_fresh_session(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": "rotated", "member_no": "42"}),
        )
        seen: list[bool] = []
        monkeypatch.setattr(
            backend,
            "_stream",
            lambda cmd, env, **kw: seen.append(backend._session().exists()) or 0,
        )

        assert backend.launch() == 0
        assert backend.launch() == 0
        assert seen == [True, True]


def _locked_delete(service, account):
    raise RuntimeError("keyring is locked")


class TestKeyringFailureSurfacing:
    def test_forget_saved_credential_propagates_and_keeps_the_cache(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        monkeypatch.setattr(fake_keyring, "delete_password", _locked_delete)

        with pytest.raises(core_secrets.SecretStoreError):
            czn_backend.forget_saved_credential("test")
        # A transient failure must not be cached as "forgotten".
        assert czn_backend._keyring_credential("test") is not None

    def test_set_remember_off_surfaces_a_failed_delete(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        monkeypatch.setattr(fake_keyring, "delete_password", _locked_delete)

        with pytest.raises(BackendError):
            backend.set_remember(False)


class TestLegacyMigrationOnKeyringFailure:
    def test_transient_failure_keeps_the_saved_login(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        path = TestLegacyStateMigration()._write_legacy(backend, launcher_refresh=SENTINEL)
        calls = {"n": 0}
        orig = fake_keyring.set_password

        def flaky(service, account, value):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("keyring is locked")
            orig(service, account, value)

        monkeypatch.setattr(fake_keyring, "set_password", flaky)

        backend._migrate_legacy_state()
        assert json.loads(path.read_text())["launcher_refresh"] == SENTINEL

        backend._migrate_legacy_state()
        assert "launcher_refresh" not in json.loads(path.read_text())
        assert _credential_blob(fake_keyring)["refresh_token"] == SENTINEL


class TestHandoffCleanup:
    def test_handoff_is_removed_even_when_the_worker_raises(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)

        def boom(module, args, **kwargs):
            handoff = (kwargs.get("extra_env") or {}).get("CZN_CREDENTIAL_OUT")
            if handoff:
                Path(handoff).parent.mkdir(parents=True, exist_ok=True)
                Path(handoff).write_text("{}")
            raise RuntimeError("worker crashed")

        monkeypatch.setattr(backend, "_run_module", boom)
        with pytest.raises(RuntimeError):
            backend._run_auth(["token", "--out", str(backend._session())])
        assert not backend._handoff_path().exists()


class TestWebView2Cancellation:
    def test_download_loop_honours_cancel(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.cancel.cancel()
        tmp = tmp_path / "inst.exe.part"

        class _Resp:
            headers = {"Content-Length": "2"}
            _done = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def geturl(self):
                return "https://go.microsoft.com/fwlink/?linkid=2124701"

            def read(self, _n=-1):
                if self._done:
                    return b""
                self._done = True
                return b"MZ"

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: _Resp())
        with pytest.raises(Cancelled):
            backend._download_webview2(
                czn_backend.WEBVIEW2_URL, tmp, tmp_path / "inst.exe"
            )
        assert tmp.stat().st_size == 0

    def test_index_fetch_honours_cancel(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        cache = tmp_path / "cache"
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        monkeypatch.setattr(backend, "_webview2_overridden", lambda: False)

        def indexed():
            backend.ctx.cancel.cancel()
            return ""

        monkeypatch.setattr(backend, "_webview2_index_source", indexed)

        def _no_net(*a, **k):
            raise AssertionError("downloaded after cancel")

        monkeypatch.setattr("urllib.request.urlopen", _no_net)
        with pytest.raises(Cancelled):
            backend._ensure_webview2()

    def test_download_cancel_propagates_through_ensure_webview2(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        cache = tmp_path / "cache"
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
        monkeypatch.setattr(backend, "_webview2_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        monkeypatch.setattr(backend, "_webview2_overridden", lambda: False)
        monkeypatch.setattr(backend, "_webview2_index_source", lambda: "")
        inst = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"

        class _Resp:
            headers = {"Content-Length": "2"}
            _done = False

            def __enter__(self):
                backend.ctx.cancel.cancel()  # Cancel pressed mid-download
                return self

            def __exit__(self, *a):
                return False

            def geturl(self):
                return "https://go.microsoft.com/fwlink/?linkid=2124701"

            def read(self, _n=-1):
                if self._done:
                    return b""
                self._done = True
                return b"MZ"

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=60: _Resp())
        with pytest.raises(Cancelled):
            backend._ensure_webview2()
        assert not inst.with_suffix(".part").exists()


class TestVerifySessionInformational:
    def test_logged_out_install_still_verifies(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        TestVerify()._full_install(backend)
        backend._session().unlink()  # install() never mints a session

        report = backend.verify()

        assert report["verified"] is True
        assert report["checks"]["session"] is False
        assert "session" not in report["failed"]
        assert "session" in report["informational"]


class TestStatusLoginRequired:
    def test_installed_logged_out_requires_login(self, make_backend_context, tmp_path):
        prefix = tmp_path / "prefix"
        _install_game(prefix)
        backend = _make_backend(make_backend_context, prefix)

        status = backend.status()

        assert status.data["logged_in"] is False
        assert status.data["login_required"] is True

    def test_not_installed_does_not_require_login(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        assert backend.status().data["login_required"] is False

    def test_logged_in_does_not_require_login(self, make_backend_context, tmp_path):
        prefix = tmp_path / "prefix"
        _install_game(prefix)
        backend = _make_backend(make_backend_context, prefix)
        backend._save_credential(SENTINEL, "42")

        status = backend.status()

        assert status.data["logged_in"] is True
        assert status.data["login_required"] is False


class TestProtonHostLibPreflight:
    def _backend(self, make_backend_context, tmp_path):
        proton = tmp_path / "MyProton"
        proton.mkdir()
        (proton / "proton").write_text("#!/bin/sh\n")
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = str(proton)
        return backend, proton

    def test_missing_32bit_libs_raise(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend, _proton = self._backend(make_backend_context, tmp_path)
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        monkeypatch.setattr(czn_backend.sandbox, "in_flatpak", lambda: False)
        monkeypatch.setattr(
            czn_backend.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"]
        )
        with pytest.raises(BackendError) as exc:
            backend._prefix_command(["game.exe"])
        assert "libfreetype.so.6" in exc.value.message

    def test_check_cached_per_backend(self, make_backend_context, tmp_path, monkeypatch):
        backend, _proton = self._backend(make_backend_context, tmp_path)
        calls: list[int] = []

        def fake_missing() -> list[str]:
            calls.append(1)
            return ["libfreetype.so.6"]

        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        monkeypatch.setattr(czn_backend.sandbox, "in_flatpak", lambda: False)
        monkeypatch.setattr(czn_backend.hostlibs, "missing_32bit", fake_missing)
        for _ in range(2):
            with pytest.raises(BackendError):
                backend._prefix_command(["game.exe"])
        assert calls == [1]

    def test_umu_present_skips_check(self, make_backend_context, tmp_path, monkeypatch):
        backend, _proton = self._backend(make_backend_context, tmp_path)
        calls: list[int] = []
        monkeypatch.setattr(
            czn_backend.shutil, "which", lambda name, *a, **k: "/usr/bin/umu-run"
        )
        monkeypatch.setattr(
            czn_backend.hostlibs,
            "missing_32bit",
            lambda: calls.append(1) or ["libfreetype.so.6"],
        )
        argv, _env = backend._prefix_command(["game.exe"])
        assert argv == ["/usr/bin/umu-run", "game.exe"]
        assert calls == []

    def test_escape_hatch_skips_check(self, make_backend_context, tmp_path, monkeypatch):
        backend, _proton = self._backend(make_backend_context, tmp_path)
        monkeypatch.setenv("NADARE_SKIP_HOSTLIBS_CHECK", "1")
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        monkeypatch.setattr(czn_backend.sandbox, "in_flatpak", lambda: False)
        monkeypatch.setattr(
            czn_backend.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"]
        )
        argv, _env = backend._prefix_command(["game.exe"])
        assert argv[-1] == "game.exe"


class TestStoppedExit:
    def test_sigterm_exit_codes_mean_stopped_not_failed(self):
        from nadare.backends.czn.backend import _STOPPED_EXIT_CODES

        assert {-15, 143, 241} <= _STOPPED_EXIT_CODES
        assert 1 not in _STOPPED_EXIT_CODES and 0 not in _STOPPED_EXIT_CODES


# ---------------------------------------------------------------------------
# CznBackend Visual C++ runtime (best-effort install; never aborts)
# ---------------------------------------------------------------------------
class TestVcredist:
    BUILTIN = b"MZ" + b"\x00" * 10 + b"Wine builtin DLL" + b"\x00" * 32

    def _backend(self, make_backend_context, tmp_path) -> CznBackend:
        return _make_backend(make_backend_context, tmp_path / "prefix")

    def _write_dlls(self, backend: CznBackend, *, native: bool = True) -> None:
        root = backend.prefix() / "drive_c/windows/system32"
        root.mkdir(parents=True, exist_ok=True)
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            (root / name).write_bytes(b"MZ-native" if native else self.BUILTIN)

    @staticmethod
    def _serve(monkeypatch, payload: bytes) -> list[str]:
        requested: list[str] = []

        class _Resp:
            headers = {"Content-Length": str(len(payload))}
            _done = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return czn_backend.VCREDIST_URL

            def read(self, _size=-1):
                if self._done:
                    return b""
                self._done = True
                return payload

        def fake_urlopen(url, timeout=60):
            requested.append(url)
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return requested

    @pytest.fixture(autouse=True)
    def _clear_pin_env(self, monkeypatch):
        monkeypatch.delenv("NADARE_VCREDIST_SHA256", raising=False)

    def test_detects_the_native_runtime(self, make_backend_context, tmp_path):
        backend = self._backend(make_backend_context, tmp_path)
        self._write_dlls(backend, native=True)
        assert backend._vcredist_installed() is True

    def test_builtin_placeholder_is_not_installed(self, make_backend_context, tmp_path):
        backend = self._backend(make_backend_context, tmp_path)
        self._write_dlls(backend, native=False)
        assert backend._vcredist_installed() is False

    def test_missing_files_are_not_installed(self, make_backend_context, tmp_path):
        backend = self._backend(make_backend_context, tmp_path)
        assert backend._vcredist_installed() is False

    def test_marker_beyond_the_first_4096_bytes_is_ignored(
        self, make_backend_context, tmp_path
    ):
        backend = self._backend(make_backend_context, tmp_path)
        late = b"MZ" + b"\x00" * 5000 + b"Wine builtin DLL"
        root = backend.prefix() / "drive_c/windows/system32"
        root.mkdir(parents=True, exist_ok=True)
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            (root / name).write_bytes(late)
        assert backend._vcredist_installed() is True

    def test_skips_when_the_native_runtime_is_present(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        self._write_dlls(backend, native=True)
        called: list[object] = []
        monkeypatch.setattr(backend, "_download_vcredist", lambda *a: called.append(a))
        monkeypatch.setattr(backend, "_run_in_prefix", lambda a: called.append(a) or 0)

        backend._ensure_vcredist()

        assert called == []

    def test_a_leftover_vcredist_false_no_longer_opts_out(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        backend.ctx.profile["vcredist"] = False
        calls: list[object] = []
        monkeypatch.setattr(czn_backend.vcredist, "ensure", lambda *a, **k: calls.append(1))

        backend._ensure_vcredist()

        assert calls == [1], "the runtime install must always be attempted for CZN"

    def test_hash_mismatch_warns_and_never_runs(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(backend, "_vcredist_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        monkeypatch.setattr(czn_backend, "VCREDIST_SHA256", "0" * 64)
        ran: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_in_prefix", lambda a: ran.append(a) or 0)
        events: list = []
        backend.ctx.emit = events.append
        self._serve(monkeypatch, b"MZ-tampered")

        backend._ensure_vcredist()  # must not raise

        assert ran == []
        assert not (cache / "vc_redist.x64.exe").exists()
        assert any(
            e.kind == "status" and "Could not verify the Microsoft Visual C++ download" in e.message
            for e in events
        ), events

    def _installable(self, make_backend_context, tmp_path, monkeypatch, payload, code):
        backend = self._backend(make_backend_context, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(czn_backend, "VCREDIST_SHA256", hashlib.sha256(payload).hexdigest())
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        state = {"installed": False}
        monkeypatch.setattr(backend, "_vcredist_installed", lambda: state["installed"])
        ran: list[list[str]] = []
        events: list = []
        backend.ctx.emit = events.append

        def fake_run(args):
            ran.append(args)
            state["installed"] = code in (0, 1638, 3010)
            return code

        monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
        self._serve(monkeypatch, payload)
        return backend, cache, ran, events

    def test_success_installs_and_invokes_the_runner_with_quiet_flags(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        payload = b"MZ-payload"
        backend, cache, ran, events = self._installable(
            make_backend_context, tmp_path, monkeypatch, payload, 0
        )

        backend._ensure_vcredist()

        assert ran and ran[0][1:] == ["/install", "/quiet", "/norestart"]
        assert (cache / "vc_redist.x64.exe").is_file()
        assert not any(e.kind == "status" for e in events)

    @pytest.mark.parametrize("code", [0, 1638, 3010])
    def test_accepted_exit_codes(
        self, make_backend_context, tmp_path, monkeypatch, code
    ):
        payload = b"MZ-ok"
        backend, _cache, ran, events = self._installable(
            make_backend_context, tmp_path, monkeypatch, payload, code
        )

        backend._ensure_vcredist()

        assert ran and ran[0][1:] == ["/install", "/quiet", "/norestart"]
        assert not any(e.kind == "status" and "not installed" in e.message for e in events)

    def test_unexpected_exit_code_is_just_a_warning(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        payload = b"MZ-code5"
        backend, _cache, ran, events = self._installable(
            make_backend_context, tmp_path, monkeypatch, payload, 5
        )

        backend._ensure_vcredist()  # must not raise

        assert ran
        assert any(
            e.kind == "status" and "not installed" in e.message for e in events
        ), events

    def test_offline_download_is_just_a_warning(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_vcredist_installed", lambda: False)
        monkeypatch.setattr(backend, "_cache_dir", lambda: tmp_path / "cache")
        events: list = []
        backend.ctx.emit = events.append

        def _offline(*a, **k):
            raise OSError("offline")

        monkeypatch.setattr("urllib.request.urlopen", _offline)
        backend._ensure_vcredist()

        assert any(
            e.kind == "status" and "not installed" in e.message for e in events
        ), events

    def test_cancelled_propagates(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._backend(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_vcredist_installed", lambda: False)

        def _cancel(*a, **k):
            raise Cancelled()

        monkeypatch.setattr(backend, "_install_vcredist", _cancel)
        with pytest.raises(Cancelled):
            backend._ensure_vcredist()

    def test_index_pin_is_accepted(self, make_backend_context, tmp_path, monkeypatch):
        payload = b"MZ-indexed"
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(czn_backend, "VCREDIST_SHA256", "0" * 64)
        monkeypatch.setattr(
            CznBackend, "_vcredist_index_pins", staticmethod(lambda: {digest})
        )
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "inst.exe"
        inst.write_bytes(payload)

        backend._verify_vcredist(inst)  # must not raise
        assert inst.exists()

    def test_unknown_hash_is_refused_and_removed(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(czn_backend, "VCREDIST_SHA256", "0" * 64)
        monkeypatch.setattr(CznBackend, "_vcredist_index_pins", staticmethod(lambda: set()))
        backend = self._backend(make_backend_context, tmp_path)
        inst = tmp_path / "inst.exe"
        inst.write_bytes(b"MZ-tampered")

        with pytest.raises(BackendError):
            backend._verify_vcredist(inst)
        assert not inst.exists()

    def test_profile_and_env_pins_are_honoured(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        data = b"MZ-native-pin"
        digest = hashlib.sha256(data).hexdigest()
        monkeypatch.setattr(czn_backend, "VCREDIST_SHA256", None)
        monkeypatch.setenv("NADARE_VCREDIST_SHA256", digest)
        backend = self._backend(make_backend_context, tmp_path)
        assert backend._vcredist_pin() == digest


@pytest.fixture(autouse=True)
def _no_vcredist_network(monkeypatch):
    """Unit tests must never fetch the real ~25 MB VC++ installer.

    Every test that exercises the download path replaces this stub in its body.
    """

    def _offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _offline)



class TestRound1AuditFixes:
    def test_rotated_token_kept_when_worker_cancelled(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)

        def boom(module, args, **kwargs):
            handoff = Path(kwargs["extra_env"]["CZN_CREDENTIAL_OUT"])
            handoff.parent.mkdir(parents=True, exist_ok=True)
            handoff.write_text(json.dumps({"refresh_token": "NEW", "member_no": "1"}))
            raise Cancelled()

        monkeypatch.setattr(backend, "_run_module", boom)
        with pytest.raises(Cancelled):
            backend._run_auth(["token", "--out", str(backend._session())])
        assert backend._refresh_token == "NEW"
        assert not backend._handoff_path().exists()

    def test_launch_removes_session_when_vcredist_cancelled(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)

        def mint():
            backend._session().write_text("{}")
            return 0

        def cancel():
            raise Cancelled()

        monkeypatch.setattr(backend, "_mint_session", mint)
        monkeypatch.setattr(backend, "_ensure_vcredist", cancel)
        with pytest.raises(Cancelled):
            backend.launch()
        assert not backend._session().exists()

    def test_verify_reads_session_info_once(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        calls: list[int] = []

        def session_info():
            calls.append(1)
            return {"valid": True}

        monkeypatch.setattr(backend, "_session_info", session_info)
        backend.verify()
        # verify() reads it once for the ``session`` check, and status() reads it
        # once more for the reported state; that is the intended count.  A
        # dropped read (0/1) or a duplicated one (3+) fails here.
        assert len(calls) == 2

    def test_installed_runner_prefers_numerically_newest(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        root = tmp_path / "runners"
        for name in ("GE-Proton9-20", "GE-Proton11-7"):
            (root / name).mkdir(parents=True)
            (root / name / "proton").write_text("#!/bin/sh\n")
        monkeypatch.setattr(backend, "_runners_dir", lambda: root)
        assert backend._installed_runner("ge").name == "GE-Proton11-7"

    def test_cancelled_non_launch_stream_does_not_stop_game(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        stopped: list[bool] = []
        monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
        backend.ctx.cancel.cancel()
        with pytest.raises(Cancelled):
            backend._stream(["sleep", "30"], {"PATH": os.environ.get("PATH", "")})
        assert stopped == []


# ---------------------------------------------------------------------------
# CznBackend.uninstall must only delete a prefix that is clearly Nadare's
# ---------------------------------------------------------------------------
class TestUninstallSafety:
    def _run(self, make_backend_context, prefix, monkeypatch=None):
        backend = _make_backend(make_backend_context, prefix)
        events = []
        backend.ctx.emit = events.append
        monkeypatch.setattr(CznBackend, "_game_running", lambda self: False)
        return backend, events

    def test_external_unmarked_prefix_is_left_in_place(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        prefix = tmp_path / "wine"
        (prefix / "drive_c" / "OtherGame").mkdir(parents=True)
        _install_game(prefix)
        backend, events = self._run(make_backend_context, prefix, monkeypatch)
        backend.uninstall()
        assert (prefix / "drive_c" / "OtherGame").is_dir()
        assert prefix.is_dir()
        assert any("left in place" in e.message for e in events)
        assert not any(e.message == "Uninstalled" for e in events)

    def test_marked_prefix_is_removed(self, make_backend_context, tmp_path, monkeypatch):
        prefix = tmp_path / "wine"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / czn_backend.PREFIX_MARKER).write_text("nadare\n")
        backend, events = self._run(make_backend_context, prefix, monkeypatch)
        backend.uninstall()
        assert not prefix.exists()
        assert any(e.kind == "done" and e.message == "Uninstalled" for e in events)

    def test_default_prefix_under_profile_dir_is_removed(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        ctx = make_backend_context("stove-czn", {})
        backend = CznBackend(ctx)
        monkeypatch.setattr(CznBackend, "_game_running", lambda self: False)
        (backend.prefix() / "drive_c").mkdir(parents=True)
        backend.uninstall()
        assert not backend.prefix().exists()

    def test_prefix_shared_with_another_profile_is_kept(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        prefix = tmp_path / "wine"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / czn_backend.PREFIX_MARKER).write_text("nadare\n")
        backend, events = self._run(make_backend_context, prefix, monkeypatch)
        backend.ctx.config.profiles["other"] = {"backend": "wine", "prefix": str(prefix)}
        backend.uninstall()
        assert (prefix / "drive_c").is_dir()
        assert any("left in place" in e.message for e in events)

    def test_failed_removal_is_reported_not_swallowed(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        prefix = tmp_path / "wine"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / czn_backend.PREFIX_MARKER).write_text("nadare\n")
        backend, events = self._run(make_backend_context, prefix, monkeypatch)

        def boom(path, *a, **k):
            raise PermissionError("locked")

        monkeypatch.setattr(czn_backend.shutil, "rmtree", boom)
        with pytest.raises(BackendError, match="locked"):
            backend.uninstall()
        assert not any(e.message == "Uninstalled" for e in events)


def test_exact_ge_proton_name_beats_a_longer_prefix_match(
    make_backend_context, tmp_path, monkeypatch
):
    """r3: "GE-Proton9-1" must not resolve to the newer "GE-Proton9-10"."""
    root = tmp_path / "runners"
    for name in ("GE-Proton9-1", "GE-Proton9-10"):
        (root / name).mkdir(parents=True)
        (root / name / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
    backend = _make_backend(make_backend_context, tmp_path / "prefix")
    backend.ctx.profile["runner"] = "GE-Proton9-1"
    assert backend.resolve_runner() == (str(root / "GE-Proton9-1"), "proton")


class TestInstallersAvoidNativeWayland:
    """Proton's native Wayland driver makes the WebView2 installer exit silently
    (exit 8, nothing installed) on some setups; the installer needs no window, so
    installers are always run with PROTON_ENABLE_WAYLAND=0."""

    def _backend(self, make_backend_context, tmp_path, monkeypatch):
        proton = tmp_path / "MyProton"
        proton.mkdir()
        (proton / "proton").write_text("#!/bin/sh\n")
        monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
        monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = str(proton)
        return backend

    def test_extra_env_can_override_the_wayland_default(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path, monkeypatch)
        _argv, env = backend._prefix_command(["game.exe"])
        assert env["PROTON_ENABLE_WAYLAND"] == "1"  # the game keeps native Wayland
        _argv, env = backend._prefix_command(
            ["setup.exe"], extra_env={"PROTON_ENABLE_WAYLAND": "0"}
        )
        assert env["PROTON_ENABLE_WAYLAND"] == "0"

    def test_webview2_install_runs_without_native_wayland(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path, monkeypatch)
        seen: list[str] = []

        def fake_stream(cmd, env, cwd=None):
            seen.append(env["PROTON_ENABLE_WAYLAND"])
            return 0

        monkeypatch.setattr(backend, "_stream", fake_stream)
        monkeypatch.setattr(backend, "_verify_webview2", lambda path: None)
        monkeypatch.setattr(backend, "_cache_dir", lambda: tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        inst = tmp_path / "cache" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        inst.write_bytes(b"MZ")
        monkeypatch.setattr(backend, "_webview2_installed", lambda: bool(seen))
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        monkeypatch.setattr(backend, "_webview2_overridden", lambda: True, raising=False)
        backend._ensure_webview2()
        assert seen == ["0"]
        # ...and the game itself still gets native Wayland afterwards.
        backend._run_in_prefix(["game.exe"])
        assert seen[-1] == "1"

    def test_vcredist_install_runs_without_native_wayland(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._backend(make_backend_context, tmp_path, monkeypatch)
        seen: list[str] = []

        def fake_install(prefix, **kwargs):
            kwargs["run_in_prefix"](["vc.exe", "/q"])

        monkeypatch.setattr(czn_backend.vcredist, "install", fake_install)
        def fake_stream(cmd, env, cwd=None):
            seen.append(env["PROTON_ENABLE_WAYLAND"])
            return 0

        monkeypatch.setattr(backend, "_stream", fake_stream)
        backend._install_vcredist()
        assert seen == ["0"]


class TestSilentPhaseHeartbeat:
    """#131: a silent installer must visibly be alive."""

    @staticmethod
    def _deterministic_clock(monkeypatch, *, step: float, silent_reads: int) -> None:
        """Drive the heartbeat off injected time instead of the wall clock."""
        clock = {"now": 0.0}
        monkeypatch.setattr(czn_backend.time, "monotonic", lambda: clock["now"])
        real_select = czn_backend.select.select
        state = {"calls": 0}

        def fake_select(rlist, wlist, xlist, timeout=None):
            if state["calls"] < silent_reads:
                state["calls"] += 1
                clock["now"] += step
                return [], [], []
            return real_select(rlist, wlist, xlist, timeout)

        monkeypatch.setattr(czn_backend.select, "select", fake_select)

    def _backend(self, make_backend_context, tmp_path):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        events: list = []
        backend.ctx.emit = events.append
        return backend, events

    def test_silent_child_emits_busy_progress_and_heartbeats(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(czn_backend, "HEARTBEAT_SECONDS", 0.3)
        self._deterministic_clock(monkeypatch, step=0.5, silent_reads=2)
        backend, events = self._backend(make_backend_context, tmp_path)
        cmd = [sys.executable, "-c", "import time; time.sleep(1.4)"]
        with backend._activity_label("Installing the WebView2 Runtime"):
            code = backend._stream(cmd, dict(os.environ))
        assert code == 0
        beats = [e for e in events if e.kind == "progress"]
        assert len(beats) == 3  # the start line plus two deterministic heartbeats
        assert all((e.data or {}).get("indeterminate") for e in beats)
        assert beats[0].message.startswith("Installing the WebView2 Runtime")
        assert any(" s " in e.message for e in beats[1:])

    def test_no_label_no_progress_events(self, make_backend_context, tmp_path, monkeypatch):
        monkeypatch.setattr(czn_backend, "HEARTBEAT_SECONDS", 0.2)
        backend, events = self._backend(make_backend_context, tmp_path)
        backend._stream([sys.executable, "-c", "import time; time.sleep(0.7)"], dict(os.environ))
        assert [e for e in events if e.kind == "progress"] == []

    def test_output_resets_the_heartbeat(self, make_backend_context, tmp_path, monkeypatch):
        monkeypatch.setattr(czn_backend, "HEARTBEAT_SECONDS", 0.6)
        self._deterministic_clock(monkeypatch, step=0.1, silent_reads=3)
        backend, events = self._backend(make_backend_context, tmp_path)
        script = (
            "import sys,time\n"
            "for _ in range(5):\n"
            "    print('x', flush=True); time.sleep(0.2)\n"
        )
        with backend._activity_label("Creating the Wine prefix"):
            backend._stream([sys.executable, "-c", script], dict(os.environ))
        beats = [e for e in events if e.kind == "progress"]
        assert len(beats) == 1  # only the start line: output kept it from going quiet


class TestLaunchStartupHeartbeat:
    """A first launch through umu can be silent for minutes: say so, but stop
    once the game has opened the launcher pipe, and never show a busy bar."""

    def test_beats_are_log_lines_and_stop_when_connected(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(czn_backend, "HEARTBEAT_SECONDS", 0.3)
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        events: list = []
        backend.ctx.emit = events.append
        log = tmp_path / "stub.log"
        state = {"connected": False}
        cmd = [sys.executable, "-c", "import time; time.sleep(2.0)"]
        import threading

        def connect_later():
            import time as _t

            _t.sleep(0.8)
            log.write_text("game connected on pipe\n")
            state["connected"] = True

        threading.Thread(target=connect_later, daemon=True).start()
        with backend._activity_label("Starting the game"):
            backend._stream(
                cmd,
                dict(os.environ),
                beat_as_log=True,
                beat_until=lambda: backend._stub_connected(log),
            )
        assert [e for e in events if e.kind == "progress"] == []  # no busy bar
        beats = [e for e in events if e.kind == "log" and e.message.startswith("Starting")]
        assert 1 <= len(beats) <= 3  # beats before the connection, none after
        assert "Steam Linux Runtime" in beats[0].message

    def test_stub_connected_reads_the_stub_log(self, tmp_path):
        log = tmp_path / "s.log"
        assert czn_backend.CznBackend._stub_connected(log) is False
        log.write_text("listening on pipe\n")
        assert czn_backend.CznBackend._stub_connected(log) is False
        log.write_text("listening\n*** game connected on pipe\n")
        assert czn_backend.CznBackend._stub_connected(log) is True


class TestRotationLock:
    """#28: two processes must not rotate the same refresh chain at once."""

    def test_second_mint_waits_then_reports_busy(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        from nadare.core.process import ProfileLock

        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential("RT1", "42")
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _auth_runner(calls))
        monkeypatch.setattr(
            backend, "_rotation_lock", lambda wait: _short(backend, wait)
        )
        with ProfileLock(backend.ctx.name):
            with pytest.raises(BackendError, match="signing this account in"):
                backend._mint_session()
        assert calls == []

    def test_mint_adopts_the_token_another_process_rotated(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential("OLD", "42")
        # Another process rotated and stored a newer token in the keyring.
        other = _cred_backend(make_backend_context, tmp_path, remember=True)
        other._save_credential("NEWER", "42")
        seen: list[str | None] = []

        def run(module, args, **kwargs):
            seen.append(backend._refresh_token)
            return 0

        monkeypatch.setattr(backend, "_run_module", run)
        backend._mint_session()
        assert seen == ["NEWER"]


def _short(backend, wait):
    # Keep the "busy" test fast: never wait the production 60 s.
    return CznBackend._rotation_lock(backend, 0.0)


class TestGameWorkingDirectoryOnC:
    """#173: the game starts on C: so the SDK's root-relative fallback is writable."""

    def test_game_dir_inside_the_prefix_is_given_as_a_c_drive_path(
        self, make_backend_context, tmp_path
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        assert backend._win_cwd(backend.game_dir()) == (
            "C:\\ProgramData\\Smilegate\\Games\\ChaosZeroNightmare"
        )
        assert backend._win_cwd(backend.prefix() / "drive_c") == "C:\\"

    def test_a_directory_outside_the_prefix_keeps_the_z_drive_form(
        self, make_backend_context, tmp_path
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        outside = tmp_path / "elsewhere" / "game"
        assert backend._win_cwd(outside) == backend._win(outside)
        assert backend._win_cwd(outside).startswith("Z:\\")

    def test_launch_hands_the_stub_a_c_drive_working_directory(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        auth = _auth_runner([], {"refresh_token": "rotated", "member_no": "42"})
        monkeypatch.setattr(backend, "_run_module", auth)
        seen: list[list[str]] = []

        def fake_prefix_command(args, extra=None):
            seen.append(list(args))
            return ["stub"], {}

        monkeypatch.setattr(backend, "_prefix_command", fake_prefix_command)
        monkeypatch.setattr(backend, "_stream", lambda cmd, env, **kw: 0)
        assert backend.launch() == 0
        stub_args = seen[-1]
        assert stub_args[2] == "--launch"
        assert stub_args[3].startswith("Z:")  # the shield executable keeps its Unix-style path
        assert stub_args[4] == "C:\\ProgramData\\Smilegate\\Games\\ChaosZeroNightmare"


class TestRunnerIntegrity:
    """A failed checksum is not an offline problem: never fall back to another build."""

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        from nadare.core import runners as core_runners

        monkeypatch.setattr(core_runners, "steam_runners", lambda: [])

    def _setup(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        backend.ctx.profile["runner"] = "dwproton"
        root = tmp_path / "runners"
        other = root / "dwproton-11.0-99-x86_64"
        other.mkdir(parents=True)
        (other / "proton").write_text("#!/bin/sh\n")
        monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
        return backend, other

    def test_a_checksum_mismatch_does_not_fall_back(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend, _other = self._setup(make_backend_context, tmp_path, monkeypatch)

        def tampered(*_a, **_k):
            raise BackendError("checksum mismatch", 9)

        monkeypatch.setattr(backend, "_download_runner", tampered)
        with pytest.raises(BackendError):
            backend.resolve_runner()

    def test_a_network_failure_still_falls_back_with_a_notice(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend, other = self._setup(make_backend_context, tmp_path, monkeypatch)

        def offline(*_a, **_k):
            raise BackendError("cannot reach github", 4)

        monkeypatch.setattr(backend, "_download_runner", offline)
        events: list = []
        backend.ctx.emit = events.append
        assert backend.resolve_runner() == (str(other), "proton")
        assert any(e.kind == "status" and "untested" in e.message for e in events)


# ---------------------------------------------------------------------------
# impl2-czn: launch/restore, remember-me, verify and auth-error wording
# ---------------------------------------------------------------------------
class TestImpl2CznFixes:
    def test_offline_restore_explains_no_internet_instead_of_log_in_first(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        _install_game(backend.prefix())
        (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ")
        monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
        # A remembered-but-not-yet-restored login whose refresh needs the network.
        backend._refresh_token = "saved-token"
        backend._member_no = "42"
        monkeypatch.setattr(backend, "_mint_session", lambda: 4)

        with pytest.raises(BackendError) as exc:
            backend.launch()

        assert exc.value.code == 4
        assert "no internet connection" in exc.value.message.lower()
        assert "log in first" not in exc.value.message
        assert backend._refresh_token is None  # keyring is untouched for next time

    def test_expired_saved_login_says_log_in_again(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        _install_game(backend.prefix())
        (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ")
        monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
        backend._refresh_token = "saved-token"
        monkeypatch.setattr(backend, "_mint_session", lambda: 3)

        with pytest.raises(BackendError) as exc:
            backend.launch()

        assert exc.value.code == 3
        assert "expired or was used elsewhere" in exc.value.message
        assert "log in first" not in exc.value.message

    def test_enabling_remember_without_a_live_login_reports_the_keyring(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("NADARE_NO_KEYRING", "1")
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        events = []
        backend.ctx.emit = events.append

        backend.set_remember(True, persist_flag=False)

        assert any(
            e.kind == "status"
            and e.message == czn_backend.REMEMBER_NOTICE
            and e.data.get("remember_unavailable")
            for e in events
        )

    def test_verify_after_login_does_not_ask_the_user_to_log_in_again(
        self, make_backend_context, tmp_path
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        prefix = backend.prefix()
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        _install_game(prefix)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ")
        webview = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0.0"
        webview.mkdir(parents=True, exist_ok=True)
        (webview / "msedgewebview2.exe").write_bytes(b"MZ")
        backend._refresh_token = "saved-token"

        report = backend.verify()

        assert "game token is created at launch" in report["message"]
        assert "log in to launch" not in report["message"]

        backend._refresh_token = None
        report = backend.verify()
        assert "not logged in; log in before launching" in report["message"]

    def test_auth_worker_error_message_reaches_the_dialog(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")

        def rate_limited(module, args, **kwargs):
            backend._emit_json_event(
                {"event": "error", "message": "[qr_status] code=429 msg=rate limited"}
            )
            return 2

        monkeypatch.setattr(backend, "_run_module", rate_limited)
        with pytest.raises(BackendError) as exc:
            backend.login()

        assert "429" in exc.value.message
        assert "QR code expired" not in exc.value.message
