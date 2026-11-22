# SPDX-License-Identifier: GPL-3.0-or-later
"""The installer-cache cleanup knows every game that uses a cache folder (#1467).

The cleanup kept its own list of which backends use which cache folder; it missed Mongil,
whose Visual C++ step (on by default) keeps its installer in the shared Wine-game folder, and
any profile that switches the step on. Those installers could be removed while still needed.
"""

from __future__ import annotations

import importlib

import pytest

from nadare.core import backends as core_backends
from nadare.core import installercache as ic


def _winegame_classes():
    from nadare.backends.winegame import WineGameBackend

    found = {}
    for kind, target in core_backends._BUILTINS.items():
        module, _, name = target.partition(":")
        try:
            cls = getattr(importlib.import_module(module), name)
        except Exception:  # noqa: BLE001 - an optional backend left out of this build
            continue
        if isinstance(cls, type) and issubclass(cls, WineGameBackend):
            found[kind] = cls
    return found


def test_mongil_keeps_the_shared_wine_game_installers():
    assert "arknights-yostar" in ic.needed_folders({"m": {"backend": "direct-mongil"}})


def test_every_backend_whose_vcredist_step_is_on_by_default_is_known():
    """The producers' own class flag and the cleanup's list must agree."""
    on_by_default = {k for k, cls in _winegame_classes().items() if cls.vcredist_default}
    assert on_by_default, "expected at least Mongil"
    missing = {
        k
        for k in on_by_default
        if "arknights-yostar" not in ic.needed_folders({"g": {"backend": k}})
    }
    assert missing == set()


@pytest.mark.parametrize("flag", [True, "true", "1", "yes"])
def test_a_profile_that_switches_vcredist_on_keeps_the_folder(flag):
    kind = next(
        k
        for k, cls in _winegame_classes().items()
        if not cls.vcredist_default and not ic.needed_folders({"g": {"backend": k}})
    )
    assert "arknights-yostar" in ic.needed_folders({"g": {"backend": kind, "vcredist": flag}})


def test_a_profile_with_vcredist_off_and_no_other_use_does_not_keep_it():
    assert ic.needed_folders({"g": {"backend": "direct-genshin", "vcredist": False}}) == set()


def test_every_wine_game_backend_uses_the_shared_folder_name():
    """The cleanup's folder name is the one WineGameBackend.cache_dir() returns."""
    from nadare.backends import winegame

    assert winegame.KIND == "arknights-yostar"
    for kind, cls in _winegame_classes().items():
        assert cls.cache_dir is winegame.WineGameBackend.cache_dir, kind
