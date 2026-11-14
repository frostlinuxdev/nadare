# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CZN login lifecycle and auth-worker hygiene (split out of test_backends_czn.py, #1248)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    _REAL_INDEX_PINS,
    _REAL_INDEX_SOURCE,
    SENTINEL,
    _auth_runner,
    _cred_backend,
    _make_backend,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _reset_czn_process_state,
    make_backend_context,
)
from _parts_patch import patch_everywhere

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import (
    CznBackend,
)
from nadare.core import indexfetch
from nadare.core.backends import BackendError


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
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT", src)
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT_SHA256", digest)

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
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT", tmp_path / "nope.exe")
        assert CznBackend._install_prebuilt_stub(tmp_path / "x.exe") is False

    def test_bundled_asset_matches_pinned_digest(self):
        import hashlib

        data = czn_backend.STUB_PREBUILT.read_bytes()
        assert hashlib.sha256(data).hexdigest() == czn_backend.STUB_PREBUILT_SHA256


class TestStubRefresh:
    """#528: a profile's stub is compared with the shipped one on every launch."""

    SHIPPED = b"MZ-shipped-stub"

    def _backend(self, make_backend_context, monkeypatch, tmp_path, *, valid=True):
        import hashlib

        src = tmp_path / "assets" / "czn_pcsdk_stub.exe"
        src.parent.mkdir()
        src.write_bytes(self.SHIPPED)
        digest = hashlib.sha256(self.SHIPPED).hexdigest() if valid else "0" * 64
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT", src)
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT_SHA256", digest)
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        messages: list[str] = []
        monkeypatch.setattr(backend, "info", lambda message, **data: messages.append(message))
        return backend, messages

    def test_a_missing_stub_is_installed_and_reported_as_shipped(
        self, make_backend_context, monkeypatch, tmp_path
    ):
        backend, messages = self._backend(make_backend_context, monkeypatch, tmp_path)
        stub = backend._ensure_stub()
        assert stub.read_bytes() == self.SHIPPED
        assert any("shipped" in m and "sha256" in m for m in messages)

    def test_the_shipped_stub_is_left_untouched(self, make_backend_context, monkeypatch, tmp_path):
        backend, messages = self._backend(make_backend_context, monkeypatch, tmp_path)
        stub = backend._ensure_stub()
        before = stub.stat().st_ino
        messages.clear()
        assert backend._ensure_stub() == stub
        assert stub.stat().st_ino == before
        assert not stub.with_name(stub.name + ".old").exists()
        assert any("shipped" in m and "differs" not in m for m in messages)

    def test_a_different_stub_is_replaced_and_the_old_one_kept(
        self, make_backend_context, monkeypatch, tmp_path
    ):
        backend, messages = self._backend(make_backend_context, monkeypatch, tmp_path)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"MZ-go-spike")
        assert backend._ensure_stub() == stub
        assert stub.read_bytes() == self.SHIPPED
        assert stub.with_name(stub.name + ".old").read_bytes() == b"MZ-go-spike"
        assert any("replaced" in m and ".old" in m for m in messages)

    def test_the_backup_is_overwritten_not_accumulated(
        self, make_backend_context, monkeypatch, tmp_path
    ):
        backend, _ = self._backend(make_backend_context, monkeypatch, tmp_path)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        old = stub.with_name(stub.name + ".old")
        old.write_bytes(b"ancient")
        stub.write_bytes(b"second")
        backend._ensure_stub()
        assert old.read_bytes() == b"second"
        assert sorted(p.name for p in stub.parent.iterdir()) == sorted([stub.name, old.name])

    def test_without_a_verified_shipped_stub_the_profile_copy_is_kept(
        self, make_backend_context, monkeypatch, tmp_path
    ):
        backend, messages = self._backend(make_backend_context, monkeypatch, tmp_path, valid=False)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"locally built")
        assert backend._ensure_stub() == stub
        assert stub.read_bytes() == b"locally built"
        assert not stub.with_name(stub.name + ".old").exists()
        assert any("differs from the shipped" in m for m in messages)

    def test_an_unreadable_stub_does_not_crash_the_launch(
        self, make_backend_context, monkeypatch, tmp_path
    ):
        backend, _ = self._backend(make_backend_context, monkeypatch, tmp_path)
        stub = backend._stub()
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(b"x")
        stub.chmod(0)
        try:
            assert backend._ensure_stub() == stub
        finally:
            stub.chmod(0o755)

    def test_stub_identity_reports_prefix_and_shipped_flag(self, monkeypatch, tmp_path):
        import hashlib

        path = tmp_path / "stub.exe"
        path.write_bytes(self.SHIPPED)
        digest = hashlib.sha256(self.SHIPPED).hexdigest()
        patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT_SHA256", digest)
        assert czn_backend.stub_identity(path) == (digest[:12], True)
        path.write_bytes(b"other")
        assert czn_backend.stub_identity(path) == (hashlib.sha256(b"other").hexdigest()[:12], False)
        assert czn_backend.stub_identity(tmp_path / "missing.exe") == (None, False)


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
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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

    def test_index_fetch_parses_and_filters(self, monkeypatch, tmp_path):
        body = json.dumps({"sha256": ["A" * 64, "bad", 5, "b" * 64]}).encode()
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("NADARE_INDEX_BASE", "https://example.test/")
        monkeypatch.setattr(indexfetch, "open_bytes", lambda *_a, **_k: body)
        assert _REAL_INDEX_PINS() == {"a" * 64, "b" * 64}

    def test_index_fetch_failure_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("NADARE_INDEX_BASE", "https://example.test/")

        def boom(*_a, **_k):
            raise OSError("offline")

        monkeypatch.setattr(indexfetch, "open_bytes", boom)
        assert _REAL_INDEX_PINS() == set()

    def test_non_https_index_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("NADARE_INDEX_BASE", "http://example.test/")

        def boom(*_a, **_k):
            raise AssertionError("a non-https base must not be fetched")

        monkeypatch.setattr(indexfetch, "open_bytes", boom)
        assert _REAL_INDEX_PINS() == set()

    def test_index_source_requires_https_microsoft(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("NADARE_INDEX_BASE", "https://example.test/")

        def serve(url):
            body = json.dumps({"url": url}).encode()
            monkeypatch.setattr(indexfetch, "open_bytes", lambda *_a, **_k: body)

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
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        # The plain-Wine fallback (no Proton runner; CZN_WINE for advanced use).
        monkeypatch.setattr(backend, "resolve_runner", lambda: ("", "wine"))
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
