# SPDX-License-Identifier: GPL-3.0-or-later
"""#811: two small hardening items.

1. A NUL inside a Steam shortcut value ended the binary-VDF string early; what followed was read
   back as new keys, so a crafted game title could inject e.g. ``LaunchOptions``.
2. ``is_secret_key`` matched only whole names, so ``nm_device_key`` (Netmarble's device secret)
   reached logs and the support report unmasked.
"""

from __future__ import annotations

import pytest

from nadare.core import shortcuts
from nadare.core.events import is_secret_key


def test_a_nul_in_a_value_cannot_inject_a_key():
    evil = "Game\x00\x01LaunchOptions\x00rm -rf ~"
    data = shortcuts.vdf_dumps([{"appid": 1, "AppName": evil, "Exe": '"/x"'}])
    (entry,) = shortcuts.vdf_loads(data)
    assert "LaunchOptions" not in entry
    assert "\x00" not in entry["AppName"] and entry["AppName"].startswith("Game")


def test_a_nul_in_a_key_is_dropped_too():
    data = shortcuts.vdf_dumps([{"appid": 1, "tags": {"0\x00\x01Exe": "t"}, "AppName": "ok"}])
    (entry,) = shortcuts.vdf_loads(data)
    assert entry["AppName"] == "ok" and "Exe" not in entry


@pytest.mark.parametrize(
    "key", ["nm_device_key", "api_key", "refresh_token", "client_secret", "db_password",
            "SGDB_API_KEY", "access_token", "password"],
)
def test_secret_shaped_keys_are_secret(key):
    assert is_secret_key(key)


@pytest.mark.parametrize("key", ["install_dir", "title", "backend", "keyboard", "monkey",
                                 "token_count_hint", "wuwa_quality"])
def test_ordinary_keys_are_not(key):
    assert not is_secret_key(key)
