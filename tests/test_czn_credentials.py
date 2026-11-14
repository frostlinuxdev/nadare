# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CZN credential hardening: no plaintext, keyring only, ephemeral session (split out of
test_backends_czn.py, #1248)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    SENTINEL,
    _auth_runner,
    _cred_backend,
    _credential_blob,
    _install_game,
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
from nadare.core import paths
from nadare.core import secrets as core_secrets
from nadare.core.backends import BackendError
from nadare.core.events import contains_value

# ---------------------------------------------------------------------------
# CZN credential hardening (no plaintext, keyring only, ephemeral session)
# ---------------------------------------------------------------------------













def _terms_runner(calls, events, credential=None, code=0):
    """Like ``_auth_runner`` but also feeds parsed JSON events to ``collect=``."""
    inner = _auth_runner(calls, credential, code, write_session=False)

    def run(module, args, **kwargs):
        result = inner(module, args, **kwargs)
        sink = kwargs.get("collect")
        if sink is not None:
            sink.extend(events)
        return result

    return run


_TERMS_DOC = {
    "no": 1502,
    "id": "STOVE_CHAOSZEROSERVICE",
    "title": "[Chaos Zero Nightmare] Terms of Service",
    "html": "<h1>Terms</h1>",
    "required": True,
}


class TestGameTerms:
    def _logged_in(self, make_backend_context, tmp_path):
        backend = _cred_backend(make_backend_context, tmp_path)
        backend._refresh_token = SENTINEL
        backend._member_no = "42"
        return backend

    def test_terms_asks_the_auth_worker_and_returns_what_is_pending(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        calls: list[list[str]] = []
        events = [{"event": "terms", "pending": True, "documents": [_TERMS_DOC]}]
        monkeypatch.setattr(
            backend,
            "_run_module",
            _terms_runner(calls, events, {"refresh_token": "ROTATED", "member_no": "42"}),
        )
        result = backend.terms()
        assert result["pending"] is True and result["documents"] == [_TERMS_DOC]
        assert result["missing"] == []
        assert calls[0][0] == "terms"
        assert backend._refresh_token == "ROTATED"  # the renewal rotated it: keep the new one

    def test_terms_passes_on_what_it_could_not_show_and_the_account_it_is_for(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        events = [{"event": "terms", "pending": True, "documents": [_TERMS_DOC], "missing": [7]}]
        monkeypatch.setattr(backend, "_run_module", _terms_runner([], events))
        result = backend.terms()
        assert result["missing"] == [7] and result["member_no"] == "42"

    def test_accept_terms_refuses_when_the_logged_in_account_is_not_the_one_shown(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _terms_runner(calls, []))
        with pytest.raises(BackendError) as exc:
            backend.accept_terms(expect_member="999")
        assert exc.value.code == 3 and calls == []
        backend.accept_terms(expect_member="42")
        assert calls and calls[0][:2] == ["accept-terms", "--agree"]

    @pytest.mark.parametrize("action", ["terms", "accept"])
    def test_a_credential_for_another_account_is_not_adopted(
        self, make_backend_context, tmp_path, monkeypatch, action
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        events = [{"event": "terms", "pending": True, "documents": [_TERMS_DOC], "missing": []}]
        monkeypatch.setattr(
            backend,
            "_run_module",
            _terms_runner([], events, {"refresh_token": "OTHER-ACCOUNT", "member_no": "999"}),
        )
        with pytest.raises(BackendError) as exc:
            backend.terms() if action == "terms" else backend.accept_terms()
        assert exc.value.code == 3
        assert backend._refresh_token != "OTHER-ACCOUNT"

    def test_terms_with_nothing_pending(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._logged_in(make_backend_context, tmp_path)
        events = [{"event": "terms", "pending": False, "documents": []}]
        monkeypatch.setattr(backend, "_run_module", _terms_runner([], events))
        result = backend.terms()
        assert result["pending"] is False and result["documents"] == []

    def test_terms_needs_a_login_and_does_not_start_the_worker(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _terms_runner(calls, []))
        with pytest.raises(BackendError) as exc:
            backend.terms()
        assert exc.value.code == 3 and calls == []

    def test_terms_failure_uses_the_auth_messages(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _terms_runner([], [], code=4))
        with pytest.raises(BackendError) as exc:
            backend.terms()
        assert exc.value.message == czn_backend.AUTH_EXIT_MESSAGES[4]

    def test_terms_without_the_expected_event_is_an_error(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _terms_runner([], [{"event": "other"}]))
        with pytest.raises(BackendError, match="terms"):
            backend.terms()

    def test_accept_terms_passes_the_explicit_agree_flag_and_keeps_the_rotated_token(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = self._logged_in(make_backend_context, tmp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            backend,
            "_run_module",
            _terms_runner(calls, [], {"refresh_token": "ROTATED", "member_no": "42"}),
        )
        backend.accept_terms()
        assert calls[0][:2] == ["accept-terms", "--agree"]
        assert backend._refresh_token == "ROTATED"

    def test_accept_terms_failure_raises(self, make_backend_context, tmp_path, monkeypatch):
        backend = self._logged_in(make_backend_context, tmp_path)
        monkeypatch.setattr(backend, "_run_module", _terms_runner([], [], code=2))
        with pytest.raises(BackendError) as exc:
            backend.accept_terms()
        assert exc.value.code == 2

    def test_accept_terms_needs_a_login(self, make_backend_context, tmp_path, monkeypatch):
        backend = _cred_backend(make_backend_context, tmp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(backend, "_run_module", _terms_runner(calls, []))
        with pytest.raises(BackendError) as exc:
            backend.accept_terms()
        assert exc.value.code == 3 and calls == []

    def test_login_that_stops_at_the_terms_keeps_the_login_and_names_the_terms(
        self, make_backend_context, tmp_path, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": "NEW", "member_no": "42"}, code=5),
        )
        with pytest.raises(BackendError) as exc:
            backend.login()
        assert exc.value.code == 5
        assert exc.value.message == CznBackend.TERMS_MESSAGE
        assert backend._refresh_token == "NEW"  # logged in; only the terms are missing

    def test_the_policy_link_is_the_one_stove_gives_for_this_game(self):
        assert CznBackend.TERMS_URL == (
            "https://policy.onstove.com/?inflow_path=LAUNCHER&show_play_button=Y"
            "&game_id=STOVE_CHAOSZERO&lang=en"
        )
        assert CznBackend.TERMS_URL in czn_backend.AUTH_EXIT_MESSAGES[5]


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
        assert "log in first, or turn on 'Remember me on this device'" in exc.value.message

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

    def test_remember_off_wins_over_a_keyring_write_already_running(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """Audit #956: a save in flight when Remember me is turned off must not leave an entry."""
        import threading

        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        started, release = threading.Event(), threading.Event()
        real_set = fake_keyring.set_password

        def slow_set(service, account, value):
            started.set()
            release.wait(5)
            real_set(service, account, value)

        monkeypatch.setattr(fake_keyring, "set_password", slow_set)
        worker = threading.Thread(target=backend._save_credential, args=(SENTINEL, "42"))
        worker.start()
        assert started.wait(5)
        backend.set_remember(False)
        release.set()
        worker.join(5)

        assert fake_keyring.store == {}
        assert czn_backend._keyring_credential("test") is None

    def test_logout_waits_for_a_renewal_that_holds_the_rotation_lock(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """Audit #957: logout must not run beside a renewal that would save the login again."""
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        order: list[str] = []

        class Lock:
            def __init__(self, profile, *, wait=0.0, poll=0.2):
                order.append(f"lock wait={wait:g}")

            def acquire(self):
                return self

            def release(self):
                order.append("unlock")

        monkeypatch.setattr(czn_backend.process, "ProfileLock", Lock)
        real_delete = backend._delete_credential

        def delete():
            order.append("delete")
            return real_delete()

        monkeypatch.setattr(backend, "_delete_credential", delete)
        backend.logout()

        assert order[0].startswith("lock") and order[-1] == "unlock" and "delete" in order

    def test_a_failed_renewal_naming_another_account_is_not_adopted(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """Audit #959: identity is checked before a rotation is kept, even after a failure."""
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        credential = {"refresh_token": "other-token", "member_no": "99"}
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], credential, code=1))

        assert backend._mint_session_unlocked() == 3

        assert czn_backend._live_get("test") is None
        assert "other-token" not in "".join(fake_keyring.store.values())

    def test_a_renewal_without_a_member_number_keeps_the_known_one(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        TestEphemeralSession()._launchable(backend, tmp_path, monkeypatch)
        credential = {"refresh_token": "next-token", "member_no": ""}
        monkeypatch.setattr(backend, "_run_module", _auth_runner([], credential))

        assert backend._mint_session_unlocked() == 0

        assert backend._member_no == "42"
        assert czn_backend._live_get("test")["member_no"] == "42"


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
        patch_everywhere(monkeypatch, czn_backend, "_registered_ephemeral", set())
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

    def test_the_stub_is_told_where_to_log_despite_the_env_cleanup(
        self, make_backend_context, tmp_path, fake_keyring, monkeypatch
    ):
        """sanitize_game_env drops every CZN_* name, which took CZN_STUB_LOG with it: the
        stub never logged, so the launch never saw the game connect and kept printing
        "Starting the game… (the first launch through umu downloads…)" while it ran."""
        backend = _cred_backend(make_backend_context, tmp_path, remember=True)
        backend._save_credential(SENTINEL, "42")
        self._launchable(backend, tmp_path, monkeypatch)
        monkeypatch.setattr(
            backend,
            "_run_module",
            _auth_runner([], {"refresh_token": "rotated", "member_no": "42"}),
        )
        seen: dict[str, str] = {}
        monkeypatch.setattr(backend, "_stream", lambda cmd, env, **kw: seen.update(env) or 0)

        assert backend.launch() == 0
        assert seen["CZN_STUB_LOG"] == backend._win(backend._stub_log())
        # The cleanup itself still holds for everything else.
        assert not [k for k in seen if k.startswith("CZN_") and k != "CZN_STUB_LOG"]

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
