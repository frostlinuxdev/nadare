# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the CZN STOVE auth module (``nadare.backends.czn.auth``).

``czn/auth.py`` is the highest-risk backend module (it decides what counts as a
secret and writes the token-bearing ``session.json``).  These tests drive it
through a fake HTTP transport so the QR/signin/renew/check plumbing and the
credential/session writers are covered without a network or a real STOVE login.
"""

from __future__ import annotations

import json
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


def test_renew_scrubs_a_legacy_plaintext_launcher_refresh_from_state(authmod, monkeypatch):
    data = {
        "code": 0,
        "value": {"access_token": "AT2", "refresh_token": "RT2", "member": {"member_no": 7}},
    }
    stove, _ = _stove(
        authmod, monkeypatch, [FakeResponse(data)], state={"launcher_refresh": "LEGACY-PLAINTEXT"}
    )
    authmod._save_state({"launcher_refresh": "LEGACY-PLAINTEXT"})
    stove.renew()
    on_disk = json.loads(authmod.STATE_FILE.read_text())
    assert "launcher_refresh" not in on_disk
    assert "LEGACY-PLAINTEXT" not in authmod.STATE_FILE.read_text()
    assert "RT2" not in authmod.STATE_FILE.read_text()


def test_main_token_exits_3_when_relogin_is_required(authmod, monkeypatch, tmp_path):
    """The backend maps exit code 3 to "log in again"; keep both sides in step."""
    stove, _ = _stove(authmod, monkeypatch)  # no refresh token -> ReloginRequired
    monkeypatch.setattr(authmod, "StoveAuth", lambda state: stove)
    assert authmod.main(["token", "--out", str(tmp_path / "session.json")]) == 3
