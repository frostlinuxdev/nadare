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


def test_login_qr_does_not_claim_a_login_without_a_credential(authmod, monkeypatch, tmp_path):
    payloads = [
        {"code": 0, "value": {"session": "s", "url": "https://stove.example/qr"}},
        {"code": 0, "value": {"status": "success"}},
        {"code": 0, "value": {}},  # sign-in "succeeded" but carried no tokens
    ]
    fake = FakeSession([FakeResponse(p) for p in payloads])
    monkeypatch.setattr(authmod, "creq", SimpleNamespace(Session=lambda *a, **k: fake))
    events = []
    monkeypatch.setattr(authmod, "event", lambda name, **fields: events.append(name))
    monkeypatch.setattr(authmod, "say", lambda *a, **k: None)
    stove = authmod.StoveAuth({})
    try:
        stove.login_qr(timeout_s=5, png_path=tmp_path / "qr.png")
    except RuntimeError:
        pass  # refusing is the correct outcome
    assert "logged_in" not in events
