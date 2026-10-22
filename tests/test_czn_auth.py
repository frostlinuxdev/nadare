# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the CZN STOVE auth module (``nadare.backends.czn.auth``).

``czn/auth.py`` is the highest-risk backend module (it decides what counts as a
secret and writes the token-bearing ``session.json``).  These tests drive it
through a fake HTTP transport so the QR/signin/renew/check plumbing and the
credential/session writers are covered without a network or a real STOVE login.
"""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class BadResponse:
    status_code = 500
    text = "<html>not json</html>"

    def json(self):
        raise ValueError("not json")


class FakeSession:
    """Minimal stand-in for ``curl_cffi.requests.Session``."""

    def __init__(self, responses) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self.responses.pop(0)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._next("POST", url, json=json, headers=headers)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._next("GET", url, params=params, headers=headers)


@pytest.fixture()
def authmod(monkeypatch, tmp_path):
    from nadare.backends.czn import auth

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(auth, "STATE_FILE", tmp_path / "state" / "czn_auth.json")
    monkeypatch.setattr(auth, "SESSION_JSON", tmp_path / "session.json")
    monkeypatch.setattr(auth, "JSON_MODE", False)
    monkeypatch.delenv("CZN_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(tmp_path / "cred-out.json"))
    return auth


def _stove(authmod, monkeypatch, responses=(), state=None):
    fake = FakeSession(responses)
    # Patch the module-local reference, never the shared curl_cffi module.
    monkeypatch.setattr(authmod, "creq", SimpleNamespace(Session=lambda *a, **k: fake))
    return authmod.StoveAuth(dict(state or {})), fake


# ---------------------------------------------------------------------------
# _apply_signin / _parse
# ---------------------------------------------------------------------------
def test_apply_signin_extracts_nested_and_list_tokens(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch)
    stove._apply_signin(
        {
            "code": 0,
            "value": {
                "access_token": "AT",
                "nested": {"refresh_token": "RT"},
                "list": [{"member_no": 42, "nickname": "neo"}],
            },
        }
    )
    assert stove.launcher_access == "AT"
    assert stove.launcher_refresh == "RT"
    assert stove.member_no == "42"


def test_parse_rejects_a_non_json_body(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch)
    with pytest.raises(RuntimeError, match="non-JSON"):
        stove._parse(BadResponse(), "tag")


# ---------------------------------------------------------------------------
# credential handoff
# ---------------------------------------------------------------------------
def test_write_credential_is_private_and_carries_the_token(authmod, monkeypatch, tmp_path):
    cred = tmp_path / "cred.json"
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(cred))
    stove, _ = _stove(authmod, monkeypatch)
    stove.launcher_refresh = "RT"
    stove.member_no = "42"
    stove.write_credential()
    assert json.loads(cred.read_text()) == {"refresh_token": "RT", "member_no": "42"}
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600


def test_write_credential_is_a_noop_without_the_env(authmod, monkeypatch, tmp_path):
    stove, _ = _stove(authmod, monkeypatch)
    stove.launcher_refresh = "RT"
    stove.write_credential()  # must not raise or write anywhere
    assert not (tmp_path / "cred.json").exists()


# ---------------------------------------------------------------------------
# renew (launcher-token refresh)
# ---------------------------------------------------------------------------
def test_renew_rotates_the_launcher_token_and_persists_it(authmod, monkeypatch, tmp_path):
    cred = tmp_path / "cred.json"
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(cred))
    data = {
        "code": 0,
        "value": {"access_token": "AT2", "refresh_token": "RT2", "member": {"member_no": 7}},
    }
    stove, fake = _stove(authmod, monkeypatch, [FakeResponse(data)])
    stove.launcher_refresh = "RT1"
    stove.renew()
    assert stove.launcher_access == "AT2"
    assert stove.launcher_refresh == "RT2"
    assert stove.member_no == "7"
    assert fake.calls[0][0] == "POST"
    assert "/sign/v2.1/pc/signin" in fake.calls[0][1]
    assert json.loads(cred.read_text())["refresh_token"] == "RT2"
    # The rotating credential must never be written to the plaintext state file.
    state_text = authmod.STATE_FILE.read_text() if authmod.STATE_FILE.exists() else ""
    assert "RT2" not in state_text


def test_renew_requires_relogin_on_code_44010(authmod, monkeypatch):
    stove, _ = _stove(
        authmod, monkeypatch, [FakeResponse({"code": 44010, "message": "expired"})]
    )
    stove.launcher_refresh = "RT"
    with pytest.raises(authmod.ReloginRequired):
        stove.renew()


def test_renew_without_a_token_requires_relogin(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch)
    with pytest.raises(authmod.ReloginRequired):
        stove.renew()


# ---------------------------------------------------------------------------
# game_check (game-token exchange)
# ---------------------------------------------------------------------------
def test_game_check_parses_tokens_and_identity(authmod, monkeypatch):
    data = {
        "code": 0,
        "value": {
            "access_token": "GAT",
            "refresh_token": "GRT",
            "member": {"member_no": 7, "guid": "MEMBER"},
            "user": {"user_id": "U1"},
        },
    }
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(data)])
    stove.launcher_access = "LAT"
    stove.game_check()
    assert stove.game_access == "GAT"
    assert stove.game_refresh == "GRT"
    assert stove.member_no == "7"
    assert stove.guid == "U1"


def test_game_check_fails_without_a_game_access_token(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 0, "value": {}})])
    stove.launcher_access = "LAT"
    with pytest.raises(RuntimeError, match="no game access_token"):
        stove.game_check()


def test_game_check_rotates_caller_detail_on_70702(authmod, monkeypatch):
    first = FakeResponse({"code": "70702", "message": "bad caller"})
    second = FakeResponse({"code": 0, "value": {"access_token": "GAT"}})
    stove, fake = _stove(authmod, monkeypatch, [first, second])
    stove.launcher_access = "LAT"
    stove.game_check()
    assert fake.calls[0][2]["headers"]["Caller-Detail"] != fake.calls[1][2]["headers"][
        "Caller-Detail"
    ]
    assert stove.game_access == "GAT"


# ---------------------------------------------------------------------------
# signin / QR
# ---------------------------------------------------------------------------
def test_signin_qr_applies_tokens(authmod, monkeypatch):
    data = {"code": 0, "value": {"access_token": "AT", "refresh_token": "RT"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(data)])
    stove.signin_qr("sess")
    assert stove.launcher_access == "AT"
    assert stove.launcher_refresh == "RT"


def test_signin_qr_propagates_the_error_code(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 123, "message": "denied"})])
    with pytest.raises(RuntimeError, match="signin"):
        stove.signin_qr("sess")


def test_login_qr_without_a_session_raises(authmod, monkeypatch, tmp_path):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 0, "value": {}})])
    with pytest.raises(RuntimeError, match="no session"):
        stove.login_qr(timeout_s=1, png_path=tmp_path / "qr.png")


def test_qr_status_returns_the_value(authmod, monkeypatch):
    payload = {"code": 0, "value": {"status": "ongoing"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(payload)])
    assert stove.qr_status("sess") == payload


# ---------------------------------------------------------------------------
# session.json assembly
# ---------------------------------------------------------------------------
def test_write_session_json_builds_a_private_session(authmod, monkeypatch, tmp_path):
    out = tmp_path / "session.json"
    stove, _ = _stove(authmod, monkeypatch, state={"member_no": "42", "guid": "G1"})
    stove.game_access = "GAT"
    stove.game_refresh = "GRT"
    stove.member = {"country_cd": "US", "nickname": "nick"}
    info = stove.write_session_json(out)
    assert info["access_token"] == "GAT"
    assert info["refresh_token"] == "GRT"
    assert info["caller_id"] == authmod.CALLER_ID
    assert info["game_id"] == authmod.GAME_ID
    assert info["guid"] == "G1"
    assert info["member_no"] == "42"
    assert info["member_nickname"] == "nick"
    assert json.loads(out.read_text()) == info
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_write_session_json_splices_into_an_existing_template(authmod, monkeypatch, tmp_path):
    out = tmp_path / "session.json"
    out.write_text(json.dumps({"access_token": "OLD", "refresh_token": "OLD_RT", "keep": "yes"}))
    stove, _ = _stove(authmod, monkeypatch)
    stove.game_access = "NEW"
    stove.game_refresh = "NEW_RT"
    info = stove.write_session_json(out)
    assert info["access_token"] == "NEW"
    assert info["refresh_token"] == "NEW_RT"
    assert info["keep"] == "yes"
    assert info["caller_id"] == authmod.CALLER_ID
    assert "expire_time" in info


def test_write_private_is_atomic_and_0600(authmod, tmp_path):
    path = tmp_path / "secret.json"
    authmod._write_private(path, '{"k": 1}')
    assert json.loads(path.read_text()) == {"k": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "secret.json.tmp").exists()


def test_session_json_carries_the_install_caller_detail(authmod, monkeypatch, tmp_path):
    """The real launcher hands the game its 40-hex install fingerprint; the SDK
    logged callerDetail empty with ours (and the game showed 'unknown error')."""
    out = tmp_path / "session.json"
    state = {"member_no": "42", "guid": "G1", "caller_detail": "ab" * 20}
    stove, _ = _stove(authmod, monkeypatch, state=state)
    stove.game_access = "GAT"
    stove.game_refresh = "GRT"
    info = stove.write_session_json(out)
    assert info["caller_detail"] == "ab" * 20

    # A template-based session gets it too, and a fresh state generates one.
    out2 = tmp_path / "s2.json"
    out2.write_text(json.dumps({"access_token": "OLD", "refresh_token": "OLD_RT"}))
    stove2, _ = _stove(authmod, monkeypatch, state={})
    stove2.game_access = "NEW"
    info2 = stove2.write_session_json(out2)
    assert len(info2["caller_detail"]) == 40
    assert set(info2["caller_detail"]) <= set("0123456789abcdef")


def test_nickname_falls_back_through_the_signin_response(authmod, monkeypatch, tmp_path):
    out = tmp_path / "session.json"
    stove, _ = _stove(authmod, monkeypatch, state={"member_no": "42", "guid": "G1"})
    stove.game_access = "GAT"
    stove.game_refresh = "GRT"
    stove.member = {"country_cd": "US"}  # no nickname here ...
    stove._signin_flat = {"nickname": "S12345"}  # ... but the sign-in had one
    assert stove.write_session_json(out)["member_nickname"] == "S12345"
    out.unlink()  # a leftover session would be reused as the template
    stove._signin_flat = {}
    assert stove.write_session_json(out)["member_nickname"] == "0"


# ---------------------------------------------------------------------------
# Audit fixes: string STOVE codes, an OSError writing session.json, and the
# QR png written 0600.
# ---------------------------------------------------------------------------
def test_game_check_accepts_a_string_zero_code(authmod, monkeypatch):
    data = {"code": "0", "value": {"access_token": "GAT"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(data)])
    stove.launcher_access = "LAT"

    stove.game_check()

    assert stove.game_access == "GAT"


def test_renew_treats_a_string_44010_as_relogin_required(authmod, monkeypatch):
    stove, _ = _stove(
        authmod, monkeypatch, [FakeResponse({"code": "44010", "message": "x"})]
    )
    stove.launcher_refresh = "RT"
    with pytest.raises(authmod.ReloginRequired):
        stove.renew()


def test_main_maps_a_session_write_oserror_to_auth_failed(authmod, monkeypatch, tmp_path):
    class _FakeAuth:
        def __init__(self, state):
            self.gds = {"nation": "US", "timezone": "UTC"}

        def renew(self):
            pass

        def game_check(self):
            pass

        def write_session_json(self, out):
            raise OSError("read-only data dir")

        def write_credential(self):
            pass

    monkeypatch.setattr(authmod, "StoveAuth", _FakeAuth)
    assert authmod.main(["token", "--out", str(tmp_path / "session.json")]) == 2


def test_login_qr_writes_the_png_private(authmod, monkeypatch, tmp_path):
    import base64

    png = tmp_path / "qr.png"
    image = base64.b64encode(b"PNG-BYTES").decode()
    create = {"code": 0, "value": {"session": "s", "url": "u", "image": image}}
    status = {"code": 0, "value": {"status": "expired"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(create), FakeResponse(status)])

    with pytest.raises(RuntimeError, match="expired"):
        stove.login_qr(timeout_s=5, png_path=png)

    assert stat.S_IMODE(png.stat().st_mode) == 0o600


def test_signin_qr_accepts_string_zero_code(authmod, monkeypatch):
    data = {"code": "0", "value": {"access_token": "AT", "refresh_token": "RT"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(data)])
    stove.signin_qr("sess")
    assert stove.launcher_access == "AT"


def test_persist_launcher_writes_handoff_before_state(authmod, monkeypatch, tmp_path):
    out = tmp_path / "cred.json"
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(out))
    stove, _ = _stove(authmod, monkeypatch, [])
    stove.launcher_refresh = "RT"

    def boom(state):
        raise OSError("read-only")

    monkeypatch.setattr(authmod, "_save_state", boom)
    with pytest.raises(OSError):
        stove._persist_launcher()
    assert "RT" in out.read_text()


def test_qr_create_with_a_non_object_value_is_not_a_crash(authmod, monkeypatch):
    """r3: a non-object ``value`` must yield {} (login_qr then reports it)."""
    stove, fake = _stove(
        authmod, monkeypatch, responses=[FakeResponse({"code": 0, "value": "oops"})]
    )
    assert stove.qr_create() == {}


# ---------------------------------------------------------------------------
# 44010 means "log in again" on every signed call, not only renew() (#90)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", [44010, "44010"])
def test_signin_qr_maps_44010_to_relogin_required(authmod, monkeypatch, code):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": code, "message": "stale"})])
    with pytest.raises(authmod.ReloginRequired):
        stove.signin_qr("sess")


@pytest.mark.parametrize("code", [44010, "44010"])
def test_game_check_maps_44010_to_relogin_required(authmod, monkeypatch, code):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": code, "message": "stale"})])
    stove.launcher_access = "LAT"
    with pytest.raises(authmod.ReloginRequired):
        stove.game_check()


def test_other_codes_are_still_plain_failures(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 123, "message": "denied"})])
    with pytest.raises(RuntimeError) as exc:
        stove.signin_qr("sess")
    assert not isinstance(exc.value, authmod.ReloginRequired)


def test_main_exits_3_when_the_session_was_invalidated(authmod, monkeypatch, tmp_path):
    """The CLI maps ReloginRequired to exit 3 ("log in again") for every step."""

    class _Auth:
        launcher_refresh = "RT"

        def renew(self):
            pass

        def game_check(self):
            raise authmod.ReloginRequired("invalidated (44010)")

    monkeypatch.setattr(authmod, "StoveAuth", lambda state: _Auth())
    monkeypatch.setattr(authmod, "_load_state", lambda: {})
    assert authmod.main(["token", "--out", str(tmp_path / "s.json")]) == 3


def test_credential_in_file_is_read_and_deleted(authmod, monkeypatch, tmp_path):
    cred = tmp_path / "credential-in.json"
    cred.write_text(json.dumps({"refresh_token": "FROM-FILE"}))
    monkeypatch.setenv("CZN_CREDENTIAL_IN", str(cred))
    monkeypatch.setenv("CZN_REFRESH_TOKEN", "FROM-ENV")
    stove = authmod.StoveAuth({})
    assert stove.launcher_refresh == "FROM-FILE"
    assert not cred.exists()


# ---------------------------------------------------------------------------
# #88 / #92: QR envelopes
# ---------------------------------------------------------------------------
def test_qr_create_rejects_an_error_envelope(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 429, "message": "slow down"})])
    with pytest.raises(RuntimeError, match="qr_create"):
        stove.qr_create()


def test_qr_create_rejects_a_non_2xx_status(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"value": {"session": "s"}}, 503)])
    with pytest.raises(RuntimeError, match="HTTP 503"):
        stove.qr_create()


def test_qr_status_rejects_an_error_envelope(authmod, monkeypatch):
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse({"code": 500, "message": "success"})])
    with pytest.raises(RuntimeError, match="qr_status"):
        stove.qr_status("sess")


def test_qr_status_keeps_terminal_states_even_with_a_nonzero_code(authmod, monkeypatch):
    payload = {"code": 1, "value": {"status": "expired"}}
    stove, _ = _stove(authmod, monkeypatch, [FakeResponse(payload)])
    assert stove.qr_status("sess") == payload


def test_login_qr_ignores_message_as_a_state(authmod, monkeypatch, tmp_path):
    stove, _ = _stove(authmod, monkeypatch)
    monkeypatch.setattr(stove, "qr_create", lambda: {"session": "s", "url": "u"})
    monkeypatch.setattr(stove, "qr_status", lambda s: {"code": 0, "message": "success"})
    signed = []
    monkeypatch.setattr(stove, "signin_qr", lambda s: signed.append(s))
    with pytest.raises(RuntimeError, match="timed out"):
        stove.login_qr(timeout_s=1, png_path=tmp_path / "qr.png")
    assert signed == []


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ((21600,), 21600),
        (("3600000",), 3600),
        ((None, 7200), 7200),
        ((0,), None),
        (("x",), None),
        ((99999999999,), None),
    ],
)
def test_ttl_seconds_reads_server_value(authmod, raw, want):
    assert authmod._ttl_seconds(*raw) == want


def test_session_json_uses_server_ttl_else_default(authmod, monkeypatch, tmp_path):
    auth, _ = _stove(authmod, monkeypatch)
    auth.game_access = "tok"
    auth.game_ttl_s = 3600
    info = auth.write_session_json(tmp_path / "a.json")
    assert info["expire_in"] == "3600" and info["expires_in"] == "3600000"
    auth.game_ttl_s = None
    info = auth.write_session_json(tmp_path / "b.json")
    assert info["expire_in"] == str(authmod.DEFAULT_TTL_S)


def test_main_fails_fast_without_credential_sink(authmod, monkeypatch, tmp_path):
    monkeypatch.delenv("CZN_CREDENTIAL_OUT", raising=False)
    for action in ("login", "token", "restore"):
        assert authmod.main([action, "--out", str(tmp_path / "s.json")]) == 2
    assert authmod.main(["status", "--out", str(tmp_path / "s.json")]) == 0


def test_credential_out_flag_is_the_opt_in_sink(authmod, monkeypatch, tmp_path):
    monkeypatch.delenv("CZN_CREDENTIAL_OUT", raising=False)
    sink = tmp_path / "sink.json"
    authmod.main(["token", "--credential-out", str(sink), "--out", str(tmp_path / "s.json")])
    assert authmod.os.environ["CZN_CREDENTIAL_OUT"] == str(sink)
    monkeypatch.delenv("CZN_CREDENTIAL_OUT", raising=False)


# ---------------------------------------------------------------------------
# impl2-czn: a missing QR image must not leave the launcher waiting
# ---------------------------------------------------------------------------
def test_login_qr_without_image_or_url_fails_immediately(authmod, monkeypatch, tmp_path):
    stove, _ = _stove(authmod, monkeypatch, [])
    monkeypatch.setattr(stove, "qr_create", lambda: {"session": "s"})

    with pytest.raises(RuntimeError, match="no QR code or login link"):
        stove.login_qr(timeout_s=300, png_path=tmp_path / "qr.png")


def test_login_qr_without_image_but_with_url_emits_the_fallback(
    authmod, monkeypatch, tmp_path
):
    stove, _ = _stove(authmod, monkeypatch, [])
    monkeypatch.setattr(
        stove, "qr_create", lambda: {"session": "s", "url": "https://stove.example/qr"}
    )
    monkeypatch.setattr(stove, "qr_status", lambda session: {"value": {"status": "expired"}})
    events = []
    monkeypatch.setattr(authmod, "event", lambda name, **fields: events.append((name, fields)))

    with pytest.raises(RuntimeError, match="expired"):
        stove.login_qr(timeout_s=5, png_path=tmp_path / "qr.png")

    qr = next(fields for name, fields in events if name == "qr")
    assert qr["png"] is None
    assert qr["url"] == "https://stove.example/qr"
