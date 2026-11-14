# SPDX-License-Identifier: GPL-3.0-or-later
from nadare.backends.nikke import hosts as h

AUTH = {
    "openid": "1",
    "token": "t",
    "pf": "p",
    "pf_key": "k",
    "bind_list": "[]",
}


def test_make_session_file_does_not_write_through_a_planted_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    target = tmp_path / "session.json"
    target.symlink_to(victim)

    h.make_session_file(
        AUTH, target, game_resource_path=r"C:\NIKKE\Unity\com_proximabeta_NIKKE"
    )

    assert victim.read_bytes() == b"precious"
    assert not target.is_symlink()
