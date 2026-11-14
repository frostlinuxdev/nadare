# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CznBackend: Visual C++ runtime install and safe uninstall (split out of test_backends_czn.py,
#1248)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import test_czn_credentials as _test_czn_credentials
from _backend_helpers import x64_dll
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    SENTINEL,
    _auth_runner,
    _cred_backend,
    _install_game,
    _make_backend,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _reset_czn_process_state,
    fake_keyring,
    make_backend_context,
)
from _parts_patch import patch_everywhere

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import (
    CznBackend,
)
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled


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
            (root / name).write_bytes(x64_dll(b"native") if native else self.BUILTIN)

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
        late = x64_dll(b"\x00" * 5000 + b"Wine builtin DLL")
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
        patch_everywhere(monkeypatch, czn_backend, "VCREDIST_SHA256", "0" * 64)
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
        patch_everywhere(
            monkeypatch, czn_backend, "VCREDIST_SHA256", hashlib.sha256(payload).hexdigest()
        )
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

    def test_a_skip_is_not_turned_into_a_day_long_failure(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        from nadare.core import vcredist as vc

        backend, _cache, _ran, events = self._installable(
            make_backend_context, tmp_path, monkeypatch, b"MZ-skip", 0
        )

        def skipped(*_a, **_k):
            raise vc.StepSkipped("the Visual C++ installer was skipped")

        monkeypatch.setattr(vc, "install", skipped)
        backend.prefix().mkdir(parents=True, exist_ok=True)
        backend._ensure_vcredist()  # the launch goes on

        marker = backend.prefix() / vc.FAILED_MARKER
        assert marker.is_file() and "skipped" in marker.read_text()
        assert any("skipped" in e.message.lower() for e in events if e.kind == "status")

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
        assert not any(e.kind == "status" and "skipped it" in e.message for e in events)

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
            e.kind == "status" and "skipped it" in e.message for e in events
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
            e.kind == "status" and "skipped it" in e.message for e in events
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
        patch_everywhere(monkeypatch, czn_backend, "VCREDIST_SHA256", "0" * 64)
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
        patch_everywhere(monkeypatch, czn_backend, "VCREDIST_SHA256", "0" * 64)
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
        patch_everywhere(monkeypatch, czn_backend, "VCREDIST_SHA256", None)
        monkeypatch.setenv("NADARE_VCREDIST_SHA256", digest)
        backend = self._backend(make_backend_context, tmp_path)
        assert backend._vcredist_pin() == digest





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
        _test_czn_credentials.TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)

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
        patch_everywhere(monkeypatch, czn_backend, "HEARTBEAT_SECONDS", 0.3)
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
        patch_everywhere(monkeypatch, czn_backend, "HEARTBEAT_SECONDS", 0.2)
        backend, events = self._backend(make_backend_context, tmp_path)
        backend._stream([sys.executable, "-c", "import time; time.sleep(0.7)"], dict(os.environ))
        assert [e for e in events if e.kind == "progress"] == []

    def test_output_resets_the_heartbeat(self, make_backend_context, tmp_path, monkeypatch):
        patch_everywhere(monkeypatch, czn_backend, "HEARTBEAT_SECONDS", 0.6)
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
        patch_everywhere(monkeypatch, czn_backend, "HEARTBEAT_SECONDS", 0.3)
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
        _test_czn_credentials.TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
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
