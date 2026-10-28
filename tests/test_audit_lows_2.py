# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit lows #592 (config hardening) and #593 (the docs match keyring-only storage)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import config as config_module
from nadare.core.config import PROFILE_DEFAULTS, Config

ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_path, profiles):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "active_profile": "a", "profiles": profiles}))
    return Config.load(path)


def test_profiles_loaded_from_disk_do_not_share_their_extra_dict(tmp_path):
    cfg = _load(tmp_path, {"a": {"backend": "wine"}, "b": {"backend": "wine"}})
    assert cfg.profiles["a"]["extra"] is not cfg.profiles["b"]["extra"]
    assert cfg.profiles["a"]["extra"] is not PROFILE_DEFAULTS["extra"]


def test_a_legacy_key_conflict_does_not_pollute_the_defaults(tmp_path):
    cfg = _load(
        tmp_path,
        {
            "a": {"backend": "wine", "proton_runner": "OLD", "runner": "NEW"},
            "b": {"backend": "wine"},
        },
    )
    assert PROFILE_DEFAULTS["extra"] == {}
    assert "legacy" not in cfg.profiles["b"]["extra"]
    assert cfg.profiles["a"]["extra"].get("legacy") == {"proton_runner": "OLD"}


def test_added_profiles_do_not_share_their_extra_dict(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    one = cfg.add_profile("one", backend="wine")
    two = cfg.add_profile("two", backend="wine")
    assert one["extra"] is not two["extra"]
    one["extra"]["x"] = 1
    assert two["extra"] == {}
    assert PROFILE_DEFAULTS["extra"] == {}


@pytest.mark.parametrize("key", ["remember_me", "accept_account_notice"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("False", False), ("true", True), ("0", False), (1, True), ("nope", False)],
)
def test_profile_booleans_mean_what_the_file_says(tmp_path, key, raw, expected):
    cfg = _load(tmp_path, {"a": {"backend": "stove-czn", key: raw}})
    assert cfg.profiles["a"][key] is expected


def test_profile_booleans_are_coerced_when_a_profile_is_added(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    prof = cfg.add_profile(
        "a", backend="stove-czn", remember_me="false", accept_account_notice="no"
    )
    assert prof["remember_me"] is False
    assert prof["accept_account_notice"] is False


def test_missing_profile_booleans_default_to_false(tmp_path):
    cfg = _load(tmp_path, {"a": {"backend": "wine"}})
    assert cfg.profiles["a"]["remember_me"] is False
    assert cfg.profiles["a"].get("accept_account_notice", False) is False


def test_the_defaults_table_itself_is_never_mutated(tmp_path):
    before = json.dumps(PROFILE_DEFAULTS, sort_keys=True)
    _load(tmp_path, {"a": {"backend": "wine", "wineprefix": "/x", "prefix": "/y"}})
    assert json.dumps(PROFILE_DEFAULTS, sort_keys=True) == before
    assert config_module.PROFILE_DEFAULTS is PROFILE_DEFAULTS


def test_the_privacy_notes_describe_keyring_only_storage():
    privacy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert "falls back to `~/.config/nadare/secrets.json`" not in privacy
    assert "Only if the keyring is unavailable" not in privacy
    assert "no plaintext fallback" in privacy.lower()
