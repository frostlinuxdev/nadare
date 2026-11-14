# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CZN launch/restore, remember-me, verify and auth-error wording (split out of
test_backends_czn.py, #1248)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    _install_game,
    _make_backend,
    _manifest_files,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _register_webview2,
    _reset_czn_process_state,
    _write_matching,
    make_backend_context,
)
from _parts_patch import patch_everywhere

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import (
    CznBackend,
)
from nadare.core import webview2 as webview2_core
from nadare.core.backends import BackendError


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

    def test_launch_stopped_by_the_terms_offers_them_instead_of_log_in_first(
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
        monkeypatch.setattr(backend, "_mint_session", lambda: 5)

        with pytest.raises(BackendError) as exc:
            backend.launch()

        assert exc.value.code == 5
        assert exc.value.message == CznBackend.TERMS_MESSAGE
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
        _register_webview2(prefix)
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


def _forge_verified(cache: Path, path: str, target: Path, md5: str) -> None:
    """Make the stat cache claim ``target`` is verified, as silent corruption would leave it."""
    info = target.stat()
    row = {
        "md5": md5,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "ino": info.st_ino,
        "dev": info.st_dev,
    }
    cache.write_text(json.dumps({"schema": 1, "files": {path: row}}))


def _corrupted_install(monkeypatch, tmp_path):
    """A game file whose content no longer matches the manifest but whose stat the cache trusts."""
    game_dir = tmp_path / "game"
    cache = tmp_path / "verified-stat.json"
    monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
    monkeypatch.setattr(czn_update, "STAT_CACHE", cache)
    data = b"verified client binary"
    md5 = _write_matching(game_dir / "a.bin", data)
    manifest = _manifest_files(f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x")
    (game_dir / "a.bin").write_bytes(b"verified client bynary")  # same size, bit flipped
    _forge_verified(cache, "/a.bin", game_dir / "a.bin", md5)
    return game_dir, cache, manifest


class TestFullVerificationIgnoresStatCache:
    def test_launch_path_trusts_the_cache(self, monkeypatch, tmp_path):
        _game_dir, _cache, manifest = _corrupted_install(monkeypatch, tmp_path)
        assert czn_update.plan(manifest)[0] == []

    def test_full_plan_hashes_and_catches_the_file(self, monkeypatch, tmp_path):
        _game_dir, cache, manifest = _corrupted_install(monkeypatch, tmp_path)
        changed, _dirs = czn_update.plan(manifest, trust_stat_cache=False)
        assert [row[0] for row in changed] == ["/a.bin"]
        assert json.loads(cache.read_text())["files"] == {}

    def test_full_plan_refreshes_the_cache_from_what_it_hashed(self, monkeypatch, tmp_path):
        game_dir, cache, manifest = _corrupted_install(monkeypatch, tmp_path)
        data = b"verified client binary"
        (game_dir / "a.bin").write_bytes(data)  # repaired
        assert czn_update.plan(manifest, trust_stat_cache=False)[0] == []
        assert "/a.bin" in json.loads(cache.read_text())["files"]

    def test_verify_action_always_hashes(self, monkeypatch, tmp_path, capsys):
        game_dir, _cache, manifest = _corrupted_install(monkeypatch, tmp_path)
        manifest_cache = tmp_path / "manifest.json"
        manifest_cache.write_text(json.dumps(manifest))
        monkeypatch.setattr(czn_update, "MANIFEST_CACHE", manifest_cache)
        assert czn_update._verify_cached() == 0
        event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert event["checked"] is True
        assert event["count"] == 1
        assert event["differ"] == ["/a.bin"]

    @pytest.mark.parametrize("flag, expected", [([], True), (["--full"], False)])
    def test_full_flag_reaches_plan(self, monkeypatch, tmp_path, flag, expected):
        game_dir, _cache, manifest = _corrupted_install(monkeypatch, tmp_path)
        seen: list[bool] = []

        def fake_plan(_manifest, trust_stat_cache=True):
            seen.append(trust_stat_cache)
            return [], []

        monkeypatch.setattr(czn_update, "plan", fake_plan)
        monkeypatch.setattr(czn_update, "local_version", lambda: 5)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda _inst: (5, "test"))
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda _target: manifest)
        monkeypatch.setattr(czn_update, "_cache_manifest", lambda _manifest: None)
        monkeypatch.setattr(czn_update, "_write_local_version", lambda *_a: None)
        assert czn_update.main(["update", "--apply", *flag]) == 0
        assert seen == [expected]


class TestRepairForcesFullVerification:
    def _backend(self, make_backend_context, tmp_path, monkeypatch):
        backend = _make_backend(make_backend_context, tmp_path / "pfx")
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_game_running", lambda: False)
        monkeypatch.setattr(
            backend, "_run_module", lambda _mod, args, **_kw: calls.append(list(args)) or 0
        )
        monkeypatch.setattr(backend, "_differing_files", lambda: [])
        return backend, calls

    def test_repair_hashes_every_file(self, make_backend_context, tmp_path, monkeypatch):
        backend, calls = self._backend(make_backend_context, tmp_path, monkeypatch)
        backend.repair()
        assert calls == [["update", "--apply", "--full"]]

    def test_plain_update_keeps_the_fast_path(self, make_backend_context, tmp_path, monkeypatch):
        backend, calls = self._backend(make_backend_context, tmp_path, monkeypatch)
        backend.update()
        assert calls == [["update", "--apply"]]


def test_upf_version_marker_is_written_atomically(monkeypatch, tmp_path, capsys):
    """#616: a failed write must leave the game's manifest as it was."""
    upf = tmp_path / "upf.json"
    original = json.dumps({"local_version": 55, "game_id": "STOVE_CHAOSZERO", "pad": "x" * 500})
    upf.write_text(original)
    monkeypatch.setattr(czn_update, "UPF", upf)

    def failing_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(czn_update.os, "replace", failing_replace)
    czn_update._write_local_version({"version_no": 56}, 56)
    assert upf.read_text() == original
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*"))  # no temp file left
    assert "could not update" in capsys.readouterr().out
    monkeypatch.undo()
    monkeypatch.setattr(czn_update, "UPF", upf)
    czn_update._write_local_version({"version_no": 56}, 56)
    assert json.loads(upf.read_text())["local_version"] == 56


class TestPrefixAndWebView2Readiness:
    """#911 / #912: a half-made prefix or half-installed runtime is not 'done'."""

    def test_a_failed_wineboot_leaves_no_prefix_to_be_trusted_next_time(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        prefix = backend.prefix()
        calls: list[list[str]] = []

        def failing_boot(args):
            calls.append(args)
            (prefix / "drive_c").mkdir(parents=True)
            return 1

        monkeypatch.setattr(backend, "_run_in_prefix", failing_boot)
        with pytest.raises(BackendError):
            backend._ensure_prefix()
        assert not prefix.exists()
        with pytest.raises(BackendError):
            backend._ensure_prefix()
        assert len(calls) == 2  # the retry ran wineboot again

    def test_a_prefix_that_already_existed_is_not_deleted_on_failure(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        prefix = backend.prefix()
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "keep.txt").write_text("mine")
        monkeypatch.setattr(backend, "_run_in_prefix", lambda args: 1)
        with pytest.raises(BackendError):
            backend._ensure_prefix()
        assert (prefix / "keep.txt").read_text() == "mine"

    def test_runtime_files_without_registration_are_not_installed(
        self, make_backend_context, tmp_path
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        app = backend.prefix() / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0"
        app.mkdir(parents=True)
        (app / "msedgewebview2.exe").write_bytes(b"MZ")
        assert webview2_core.installed(backend.prefix())
        assert backend._webview2_installed() is False

    def test_ensure_does_not_stop_at_runtime_files_that_are_not_registered(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        app = backend.prefix() / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0"
        app.mkdir(parents=True)
        (app / "msedgewebview2.exe").write_bytes(b"MZ")
        monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
        patch_everywhere(monkeypatch, czn_backend, "WEBVIEW2_SHA256", None)
        with pytest.raises(BackendError):  # it went on to the repair, which has no pin here
            backend._ensure_webview2()


class TestFailedMintLeavesNoSession:
    """#916: a token mint that fails after writing session.json must not leave it behind."""

    @pytest.mark.parametrize("handoff", [None, {"refresh_token": "r", "member_no": "1"}])
    def test_a_failed_mint_removes_the_session_file(
        self, make_backend_context, tmp_path, monkeypatch, handoff
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        session = backend._session()
        session.parent.mkdir(parents=True, exist_ok=True)

        def fake_auth(args):
            session.write_text('{"access_token": "live"}')
            return 2, handoff

        monkeypatch.setattr(backend, "_run_auth", fake_auth)
        monkeypatch.setattr(backend, "_save_credential", lambda *a, **k: None)
        monkeypatch.setattr(backend, "_adopt_newer_credential", lambda: None)
        assert backend._mint_session() == 2
        assert not session.exists()

    def test_a_good_mint_keeps_the_session_file(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _make_backend(make_backend_context, tmp_path / "prefix")
        session = backend._session()
        session.parent.mkdir(parents=True, exist_ok=True)

        def fake_auth(args):
            session.write_text('{"access_token": "live"}')
            return 0, {"refresh_token": "r", "member_no": "1"}

        monkeypatch.setattr(backend, "_run_auth", fake_auth)
        monkeypatch.setattr(backend, "_save_credential", lambda *a, **k: None)
        monkeypatch.setattr(backend, "_adopt_newer_credential", lambda: None)
        assert backend._mint_session() == 0
        assert session.exists()
