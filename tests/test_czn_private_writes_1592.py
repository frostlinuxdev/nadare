# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""#1592 item 1: CZN's private credential writes never follow a planted symlink."""

from __future__ import annotations

import json
import stat

from _czn_test_helpers import (  # noqa: F401 - pytest fixtures, found by name
    SENTINEL,
    _cred_backend,
    _reset_czn_process_state,
    fake_keyring,
    make_backend_context,
)

from nadare.backends.czn.backend_parts import credentials


def _regular_0600(path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600


def test_a_planted_tmp_link_is_not_written_through(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path = tmp_path / "state.json"
    (tmp_path / "state.json.tmp").symlink_to(victim)

    credentials._write_private(path, '{"a": 1}')

    assert victim.read_text() == "keep"
    assert _regular_0600(path) and path.read_text() == '{"a": 1}'


def test_a_link_at_the_target_is_replaced_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path = tmp_path / "state.json"
    path.symlink_to(victim)

    credentials._write_private(path, "{}")

    assert victim.read_text() == "keep"
    assert _regular_0600(path)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_staged_credential_never_writes_through_a_link(make_backend_context, tmp_path):
    backend = _cred_backend(make_backend_context, tmp_path)
    backend._refresh_token = SENTINEL
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path = backend._input_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(victim)

    backend._stage_input_credential()

    assert victim.read_text() == "keep"
    assert _regular_0600(path)
    assert json.loads(path.read_text()) == {"refresh_token": SENTINEL}
