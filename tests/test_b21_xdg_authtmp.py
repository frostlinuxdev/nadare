# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""XDG variables must be absolute (per the spec); CZN state writes must not share a temp name."""

from __future__ import annotations

import stat

import pytest

from nadare.backends.czn import auth
from nadare.core import paths


@pytest.mark.parametrize(
    ("env", "fn", "default"),
    [
        ("XDG_DATA_HOME", paths.data_dir, ".local/share"),
        ("XDG_CONFIG_HOME", paths.config_dir, ".config"),
        ("XDG_STATE_HOME", paths.state_dir, ".local/state"),
        ("XDG_CACHE_HOME", paths.cache_dir, ".cache"),
    ],
)
def test_relative_xdg_variable_is_ignored(monkeypatch, tmp_path, env, fn, default):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(env, "relative/dir")
    assert fn() == tmp_path / default / "nadare"


def test_absolute_xdg_variable_is_used(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "abs"))
    assert paths.data_dir() == tmp_path / "abs" / "nadare"


def test_private_write_does_not_use_a_fixed_tmp_name(tmp_path):
    tmp_path = tmp_path / "d"
    tmp_path.mkdir()
    target = tmp_path / "state.json"
    squatter = tmp_path / "state.json.tmp"
    squatter.write_text("someone else's file")
    auth._write_private(target, "mine")
    assert target.read_text() == "mine"
    assert squatter.read_text() == "someone else's file"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.tmp"]


def test_private_bytes_write_is_atomic_and_private(tmp_path):
    tmp_path = tmp_path / "d"
    tmp_path.mkdir()
    target = tmp_path / "qr.png"
    auth._write_private_bytes(target, b"\x89PNG")
    assert target.read_bytes() == b"\x89PNG"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["qr.png"]
