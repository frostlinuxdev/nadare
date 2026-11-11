# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b003: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

from types import SimpleNamespace

import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)

    def post(self, url, json=None, headers=None, timeout=None):
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        return self.responses.pop(0)


@pytest.fixture()
def authmod(monkeypatch, tmp_path):
    from nadare.backends.czn import auth

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(auth, "STATE_FILE", tmp_path / "state" / "czn_auth.json")
    monkeypatch.setattr(auth, "JSON_MODE", False)
    monkeypatch.delenv("CZN_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("CZN_CREDENTIAL_IN", raising=False)
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(tmp_path / "cred.json"))
    return auth


def test_a_non_object_signin_value_does_not_escape_as_attributeerror(authmod, monkeypatch):
    payload = {"code": 0, "access_token": "AT", "refresh_token": "RT", "value": "oops"}
    fake = FakeSession([FakeResponse(payload)])
    monkeypatch.setattr(authmod, "creq", SimpleNamespace(Session=lambda *a, **k: fake))
    stove = authmod.StoveAuth({})
    try:
        stove.signin_qr("sess")
    except RuntimeError:
        return  # a clean rejection is also fine
    # Both tokens were seen before the crash, so a non-object value must not abort.
    assert stove.launcher_refresh == "RT"
