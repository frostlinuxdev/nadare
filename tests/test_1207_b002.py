import pytest


@pytest.fixture()
def authmod(monkeypatch, tmp_path):
    from nadare.backends.czn import auth

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(auth, "STATE_FILE", tmp_path / "state" / "czn_auth.json")
    monkeypatch.setattr(auth, "SESSION_JSON", tmp_path / "session.json")
    monkeypatch.setattr(auth, "JSON_MODE", False)
    monkeypatch.delenv("CZN_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("CZN_CREDENTIAL_IN", raising=False)
    monkeypatch.setenv("CZN_CREDENTIAL_OUT", str(tmp_path / "cred.json"))
    return auth


def test_a_state_file_that_is_json_but_not_an_object_does_not_crash(authmod):
    # A hand-edited / older writer's czn_auth.json: valid JSON, wrong type.
    authmod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    authmod.STATE_FILE.write_text("[]")

    assert authmod.main(["status"]) == 0
