# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CZN file-level verify and the audit fixes: exit-code tables, launch minting, keyring failures,
credential hand-off, WebView2 cancel (split out of test_backends_czn.py, #1248)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_czn_backend_basics as _test_czn_backend_basics
import test_czn_credentials as _test_czn_credentials
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    SENTINEL,
    _auth_runner,
    _cred_backend,
    _credential_blob,
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
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import (
    SERVICE,
)
from nadare.core import secrets as core_secrets
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled


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
        _test_czn_backend_basics.TestVerify()._full_install(backend)
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

    def test_login_maps_exit_5_to_the_game_terms_message(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], None, code=5))
        with pytest.raises(BackendError) as exc:
            backend.login()
        assert exc.value.code == 5
        assert exc.value.message == czn_backend.AUTH_EXIT_MESSAGES[5]
        assert "terms" in exc.value.message.lower()
        assert "try again in a minute" not in exc.value.message

    def test_launch_token_refresh_uses_the_auth_table(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        _test_czn_credentials.TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
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
        _test_czn_credentials.TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
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
        path = _test_czn_credentials.TestLegacyStateMigration()._write_legacy(
            backend, launcher_refresh=SENTINEL
        )
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
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", "0" * 64)
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
        _test_czn_backend_basics.TestVerify()._full_install(backend)
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
