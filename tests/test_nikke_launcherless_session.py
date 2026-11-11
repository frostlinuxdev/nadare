# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the NIKKE Sail session helper (``nadare.backends.nikke.session``).

The transformation and the constants come from a real ``cmd=100`` capture; the
game rejects the INTL service blob unchanged (``kErrorInitializeReadInitializeInfoFailed``)
and accepts it with top-level ``bind_list`` emptied.
"""

from __future__ import annotations

import json

import pytest

from nadare.backends.nikke import session as sail


def auth_blob() -> dict:
    return {
        "ret": 0,
        "sub_ret": -1,
        "msg": "success",
        "ret_code": 0,
        "openid": "10000000000000000001",
        "token": "0123456789abcdef0123456789abcdef01234567",
        "channelid": 131,
        "pf": "LevelInfinite_Example",
        "pf_key": "00000000000000000000000000000000",
        "bind_list": json.dumps([{"channelId": 131, "user_name": "Player_Test"}]),
        "extra_json": json.dumps({"bindlist_msg": "success"}),
        "token_expire_time": 1796582282,
    }


def test_trim_game_auth_removes_top_level_bind_list_and_compacts():
    auth = auth_blob()
    encrypted = sail.trim_game_auth(auth)

    parsed = json.loads(encrypted)
    assert parsed["bind_list"] == ""
    assert parsed["openid"] == auth["openid"]
    assert parsed["token"] == auth["token"]
    assert parsed["extra_json"] == auth["extra_json"]
    # outer serialization is compact (spaces may exist inside string values)
    assert encrypted == json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    # the captured game-auth blob for this account was 8041 bytes; removing the
    # bind list must at least shrink the service blob.
    assert len(encrypted) < len(json.dumps(auth, ensure_ascii=False))


def test_trim_game_auth_rejects_missing_credentials():
    auth = auth_blob()
    del auth["token"]
    with pytest.raises(ValueError, match="token"):
        sail.trim_game_auth(auth)


def test_build_session_uses_captured_constants_and_preserves_credentials():
    auth = auth_blob()
    built = sail.build_session(auth, game_resource_path=r"S:\NIKKE\Unity\com_proximabeta_NIKKE")

    assert built["game_id"] == 29080
    assert built["launcher_game_id"] == 16601
    assert built["channel_id"] == 131
    assert built["zone_id"] == 10001
    assert built["platform_language_code"] == 1033
    assert built["platform_type"] == 1
    assert built["compliance_region"] == "250"
    assert built["branch_id"] == "1"
    assert built["version_id"] == "0.0.0.74"
    assert built["distribute_id"] == "0"
    assert built["game_resource_path"] == r"S:\NIKKE\Unity\com_proximabeta_NIKKE"
    assert built["open_id"] == auth["openid"]
    assert built["token"] == auth["token"]
    assert built["pf"] == auth["pf"]
    assert built["pf_key"] == auth["pf_key"]
    assert json.loads(built["encypt_data"])["bind_list"] == ""


@pytest.mark.parametrize("odd", [None, "x", [], {}])
def test_an_odd_channel_id_falls_back_to_the_default(odd):
    """Fuzz 2026-11-11: channelid null raised a bare TypeError out of launch."""
    from nadare.backends.nikke import session

    auth = {"openid": "1", "token": "t", "pf": "p", "pf_key": "k", "channelid": odd}
    assert session.build_session(auth)["channel_id"] == session.CHANNEL_ID
