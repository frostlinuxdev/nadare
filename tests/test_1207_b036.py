# SPDX-License-Identifier: GPL-3.0-or-later
"""A non-finite channelid from the sign-in JSON aborts the launch with OverflowError."""

from __future__ import annotations

import json

from nadare.backends.nikke import session as sail


def auth_blob(channelid):
    return {
        "openid": "10000000000000000001",
        "token": "0123456789abcdef0123456789abcdef01234567",
        "pf": "LevelInfinite_Example",
        "pf_key": "00000000000000000000000000000000",
        "channelid": channelid,
    }


def test_huge_json_channel_id_falls_back_to_the_default():
    # nikke.hosts.read_auth uses json.loads, which turns 1e400 into float('inf').
    auth = json.loads(
        '{"openid":"1","token":"t","pf":"p","pf_key":"k","channelid":1e400}'
    )
    assert auth["channelid"] == float("inf")  # the state the host reply produces

    built = sail.build_session(auth)
    assert built["channel_id"] == sail.CHANNEL_ID


def test_infinite_channel_id_falls_back_to_the_default():
    built = sail.build_session(auth_blob(float("inf")))
    assert built["channel_id"] == sail.CHANNEL_ID
