# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Netmarble login of the Mongil: Star Dive backend."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

import pytest
from _backend_helpers import make_context, make_jwt, write_exe

from nadare.backends import arknights as ak
from nadare.backends import direct, nmlogin
from nadare.core import netmarble as nm
from nadare.core.backends import BackendError, supports_login, supports_remember_me
from nadare.core.events import Cancel, Cancelled
from nadare.core.secrets import SecretStoreError


class FakeStore:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.secure = True
        self.gets = 0
        self.fail = False

    def get(self, profile):
        self.gets += 1
        return self.data.get(profile)

    def set(self, profile, token):
        if self.fail:
            raise SecretStoreError("locked")
        self.data[profile] = token

    def delete(self, profile):
        if self.fail:
            raise SecretStoreError("locked")
        self.data.pop(profile, None)


@pytest.fixture(autouse=True)
def _fresh_state():
    nmlogin.reset_process_state()
    yield
    nmlogin.reset_process_state()


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(nmlogin, "_store", lambda: fake)
    return fake


def make_backend(tmp_path, profile=None, name="mongil", installed=True):
    values = {"prefix": str(tmp_path / "pfx"), **(profile or {})}
    backend = direct.MongilBackend(make_context(tmp_path, "direct-mongil", values, name))
    if installed:
        write_exe(backend.install_dir() / "STARDIVE.exe", "MZ")
    backend.events = []
    backend.ctx.emit = backend.events.append
    return backend


def browser_that_signs_in(token, delay=0.1):
    """A stand-in for opening the browser: the 'user' finishes sign-in on the redirect URL."""

    def open_browser(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        redirect = q["redirectUrl"]  # Netmarble's page appends accessToken to this URL
        target = (
            redirect
            + ("&" if "?" in redirect else "?")
            + urllib.parse.urlencode({"accessToken": token})
        )

        def go():
            time.sleep(delay)
            urllib.request.urlopen(target, timeout=5).read()

        threading.Thread(target=go, daemon=True).start()
        return True

    return open_browser


@pytest.fixture
def sign_in(monkeypatch):
    def apply(token, name="Cat"):
        monkeypatch.setattr(direct.MongilBackend, "_open_browser", browser_that_signs_in(token))
        monkeypatch.setattr(nm, "fetch_user_name", lambda t, fetch=None: name)

    return apply


# ------------------------------------------------------------------ wiring
def test_the_mongil_kind_advertises_login_and_remember_me():
    assert supports_login("direct-mongil") and supports_remember_me("direct-mongil")
    assert not supports_login("direct-bd2")


# ------------------------------------------------------------------ login
def test_login_round_trip(tmp_path, store, sign_in):
    token = make_jwt("P77")
    sign_in(token)
    b = make_backend(tmp_path)
    assert b.status().data["logged_in"] is False
    b.login()
    data = b.status().data
    assert data["logged_in"] is True and data["account"] == "Cat" and data["login_expired"] is False
    assert any(e.kind == "done" and "Cat" in e.message for e in b.events)
    assert store.data == {}  # remember me is off: nothing persisted
    assert nm.new_device_key() != b.ctx.profile["nm_device_key"]  # a key was generated and saved
    assert len(b.ctx.profile["nm_device_key"]) == 36


def test_the_device_key_is_stable_across_logins(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    b.login()
    first = b.ctx.profile["nm_device_key"]
    b.login()
    assert b.ctx.profile["nm_device_key"] == first


def test_login_url_is_shown_when_no_browser_opens(tmp_path, store, monkeypatch):
    monkeypatch.setattr(direct.MongilBackend, "_open_browser", lambda self, url: False)
    b = make_backend(tmp_path)
    b.login_timeout = 0.3
    with pytest.raises(BackendError, match="(?i)sign-in"):
        b.login()
    shown = [e for e in b.events if e.data.get("login_url")]
    assert shown and shown[0].data["login_url"].startswith("https://members.netmarble.com/auth?")
    assert shown[0].data["login_url"] in shown[0].message


def test_login_timeout_is_a_backend_error(tmp_path, store, monkeypatch):
    monkeypatch.setattr(direct.MongilBackend, "_open_browser", lambda self, url: True)
    b = make_backend(tmp_path)
    b.login_timeout = 0.3
    with pytest.raises(BackendError) as ei:
        b.login()
    assert ei.value.code == 3 and b.status().data["logged_in"] is False


def test_login_can_be_cancelled(tmp_path, store, monkeypatch):
    monkeypatch.setattr(direct.MongilBackend, "_open_browser", lambda self, url: True)
    b = make_backend(tmp_path)
    cancel = Cancel()
    b.ctx.cancel = cancel
    threading.Timer(0.2, cancel.cancel).start()
    with pytest.raises(Cancelled):
        b.login()


def test_an_expired_token_from_the_browser_is_refused(tmp_path, store, sign_in):
    sign_in(make_jwt(exp=int(time.time()) - 100))
    b = make_backend(tmp_path)
    with pytest.raises(BackendError, match="(?i)expired"):
        b.login()
    assert b.status().data["logged_in"] is False


# ------------------------------------------------------------------ remember me
def test_remember_me_keeps_the_login_in_the_keyring_only(tmp_path, store, sign_in):
    token = make_jwt("P5")
    sign_in(token)
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    saved = json.loads(store.data["mongil"])
    assert saved["token"] == token and saved["player_id"] == "P5" and saved["device_key"]
    assert not list(tmp_path.rglob("*.json")) or token not in "".join(
        p.read_text() for p in tmp_path.rglob("*.json") if "config" in p.name
    )


def test_a_remembered_login_survives_a_restart_and_is_read_once(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    nmlogin.reset_process_state()  # a new process
    b2 = make_backend(tmp_path, {"remember_me": True})
    for _ in range(5):
        assert b2.status().data["logged_in"] is True
    assert store.gets == 1  # status() polling must not hammer the keyring


def test_keyring_read_failure_is_retried_on_the_next_session_lookup(tmp_path, monkeypatch):
    session = nm.Session(
        token=make_jwt("P9"),
        expires_at=int(time.time()) + 3600,
        player_id="P9",
        device_key="0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d",
        name="Cat",
    )

    class FailOnceStore:
        calls = 0

        def get(self, profile):
            self.calls += 1
            if self.calls == 1:
                raise SecretStoreError("temporarily locked")
            assert profile == "mongil"
            return nmlogin._encode(session)

    store = FailOnceStore()
    monkeypatch.setattr(nmlogin, "_store", lambda: store)
    backend = make_backend(tmp_path, {"remember_me": True})

    assert backend._session() is None
    assert backend._session() == session
    assert store.calls == 2


def test_without_remember_me_a_restart_forgets_the_login(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    b.login()
    nmlogin.reset_process_state()
    assert make_backend(tmp_path).status().data["logged_in"] is False
    assert store.gets == 0


def test_turning_remember_me_on_saves_the_live_session(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    b.login()
    assert store.data == {}
    b.set_remember(True)
    assert "mongil" in store.data and b.ctx.profile["remember_me"] is True


def test_turning_remember_me_off_wipes_the_keyring(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    b.set_remember(False)
    assert store.data == {}
    nmlogin.reset_process_state()
    assert make_backend(tmp_path).status().data["logged_in"] is False


def test_a_locked_keyring_is_reported_when_forgetting(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    store.fail = True
    with pytest.raises(BackendError):
        b.set_remember(False)


def test_an_unusable_keyring_does_not_break_login(tmp_path, store, sign_in):
    sign_in(make_jwt())
    store.secure = False
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    assert b.status().data["logged_in"] is True
    assert any(e.data.get("remember_unavailable") for e in b.events)


def test_logout_clears_everything(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    b.logout()
    assert store.data == {} and b.status().data["logged_in"] is False
    assert any(e.kind == "done" and "Logged out" in e.message for e in b.events)


def test_forget_saved_login_is_static_and_wipes_the_entry(tmp_path, store, sign_in):
    sign_in(make_jwt())
    make_backend(tmp_path, {"remember_me": True}).login()
    direct.MongilBackend.forget_saved_login("mongil")
    assert store.data == {}


def test_forget_saved_login_also_clears_the_live_session(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    b.login()
    assert b._session() is not None
    direct.MongilBackend.forget_saved_login("mongil")
    assert b._session() is None  # a same-name profile cannot reuse the live token


def test_logout_with_a_failed_keyring_delete_does_not_claim_plain_success(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path, {"remember_me": True})
    b.login()
    store.fail = True
    b.logout()
    assert b.status().data["logged_in"] is False
    assert any(e.kind == "status" and "could not be removed" in e.message for e in b.events)
    done = [e for e in b.events if e.kind == "done"]
    assert done and "will return after a restart" in done[-1].message
    assert not any(e.kind == "done" and e.message == "Logged out of Netmarble" for e in b.events)


def test_a_corrupt_keyring_entry_means_logged_out(tmp_path, store):
    store.data["mongil"] = "not json"
    assert make_backend(tmp_path, {"remember_me": True}).status().data["logged_in"] is False


# ------------------------------------------------------------------ launch
def test_launch_needs_a_login(tmp_path, store):
    b = make_backend(tmp_path)
    with pytest.raises(BackendError) as ei:
        b.launch()
    assert ei.value.code == 3 and "Log in" in str(ei.value)


def test_launch_of_an_uninstalled_game_is_not_a_login_error(tmp_path, store):
    b = make_backend(tmp_path, installed=False)
    with pytest.raises(BackendError, match="not found"):
        b.launch()


def test_launch_with_an_expired_login_says_so(tmp_path, store):
    b = make_backend(tmp_path)
    nmlogin._LIVE["mongil"] = nm.Session(
        make_jwt(), int(time.time()) - 10, "P", "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
    )
    with pytest.raises(BackendError, match="(?i)expired"):
        b.launch()
    assert b.status().data["login_expired"] is True and b.status().data["logged_in"] is False


@pytest.fixture
def launch_spy(monkeypatch):
    seen: dict = {}

    def fake_prefix_command(self, args, extra_env=None):
        seen["args"], seen["extra_env"] = list(args), dict(extra_env or {})
        return ["umu-run", *args], dict(extra_env or {})

    monkeypatch.setattr(ak.ArknightsBackend, "_prefix_command", fake_prefix_command)
    monkeypatch.setattr(ak.ArknightsBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(ak.ArknightsBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)
    return seen


def test_launch_hands_the_game_its_token_but_never_logs_it(tmp_path, store, sign_in, launch_spy):
    token = make_jwt("P9")
    sign_in(token)
    b = make_backend(tmp_path, {"launch_args": "-windowed"})
    b.login()
    assert b.launch() == 0
    args = launch_spy["args"]
    assert args[0].endswith("STARDIVE.exe") and "-windowed" in args
    for a in (
        "NMAUTH_TYPE=netmarble",
        "-NMAUTH_TYPE=netmarble",
        f"NMAUTH_TOKEN={token}",
        f"-NMAUTH_TOKEN={token}",
        "NMENV=nmp",
        "-NMENV=nmp",
    ):
        assert a in args
    assert args.index("-windowed") < args.index("NMAUTH_TYPE=netmarble")  # profile args first
    env = launch_spy["extra_env"]
    assert env["NMAUTH_TOKEN"] == token and env["NMDeviceKey"] == b.ctx.profile["nm_device_key"]
    assert env["NMAUTH_TYPE"] == "netmarble" and env["NMENV"] == "nmp"
    for event in b.events:
        assert token not in event.message, event


def test_launch_syncs_the_device_key_into_the_prefix(tmp_path, store, sign_in, launch_spy):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    (tmp_path / "pfx").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pfx" / "user.reg").write_text("WINE REGISTRY Version 2\n\n#arch=win64\n")
    b.login()
    b.launch()
    reg = (tmp_path / "pfx" / "user.reg").read_text()
    assert f'"NMDeviceKey"="{b.ctx.profile["nm_device_key"]}"' in reg


def test_launch_without_a_prefix_registry_still_works(tmp_path, store, sign_in, launch_spy):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    b.login()
    assert b.launch() == 0  # no user.reg yet: the sync is skipped quietly


def test_other_games_launch_unchanged(tmp_path, monkeypatch):
    seen = {}

    def fake_prefix_command(self, args, extra_env=None):
        seen["args"], seen["extra_env"] = list(args), extra_env
        return ["umu-run", *args], {}

    monkeypatch.setattr(ak.ArknightsBackend, "_prefix_command", fake_prefix_command)
    monkeypatch.setattr(ak.ArknightsBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(ak.ArknightsBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)
    b = direct.BrownDust2Backend(
        make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    )
    write_exe(b.install_dir() / "BrownDust II.exe", "MZ")
    assert b.launch() == 0
    assert len(seen["args"]) == 1 and seen["extra_env"] is None


def test_a_drive_by_request_without_the_state_cannot_log_you_in(tmp_path, store, monkeypatch):
    """A web page that guesses the port must not be able to plant its own token."""
    attacker = make_jwt("ATTACKER")

    def open_browser(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        base = q["redirectUrl"].split("?")[0]

        def go():
            time.sleep(0.1)
            try:
                urllib.request.urlopen(
                    base + "?" + urllib.parse.urlencode({"accessToken": attacker}), timeout=5
                )
            except Exception:
                pass  # 403 is the expected answer

        threading.Thread(target=go, daemon=True).start()
        return True

    monkeypatch.setattr(direct.MongilBackend, "_open_browser", open_browser)
    b = make_backend(tmp_path)
    b.login_timeout = 0.6
    with pytest.raises(BackendError):
        b.login()
    assert b.status().data["logged_in"] is False


def test_status_asks_for_a_login_only_once_the_game_is_installed(tmp_path, store, sign_in):
    sign_in(make_jwt())
    b = make_backend(tmp_path)
    assert b.status().data["login_required"] is True
    b.login()
    assert b.status().data["login_required"] is False
    assert (
        make_backend(tmp_path / "other", installed=False, name="m2").status().data["login_required"]
        is False
    )
