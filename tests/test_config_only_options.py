# SPDX-License-Identifier: GPL-3.0-or-later
"""Support and developer overrides stay honoured in the profile but are not shown (#778)."""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import CONFIG_ONLY_KEYS, config_only, get, kinds
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage

DOC = Path(__file__).resolve().parents[1] / "docs" / "CONFIG-ONLY-OPTIONS.md"
EXPECTED = {
    "installer_url", "installer_sha256", "installer_args", "manifest_index", "manifest_file",
    "media_file", "kuro_token", "repo", "mode", "size", "release", "data_dir", "logs_dir",
    "desktop_dir", "cache", "adopt", "force_update", "purge_cache", "uninstall_all",
    "wine_binary", "umu_binary", "download_workers",
}  # fmt: skip


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_the_hidden_keys_are_exactly_the_agreed_list():
    assert set(CONFIG_ONLY_KEYS) == EXPECTED


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_a_listed_key_is_config_only_on_every_backend(key):
    assert config_only("direct-bd2", key) and config_only("script", key)


@pytest.mark.parametrize(
    "key", ["extra_env", "steam_binary",
            "stop_steam", "launch_args", "prefix", "runner", "install_dir", "monitor"],
)  # fmt: skip
def test_the_options_users_need_stay_visible(key):
    assert not config_only("direct-bd2", key)


def test_process_match_is_shown_for_script_games_only():
    assert not config_only("script", "process_match")
    assert config_only("wine", "process_match")
    assert config_only("steam", "process_match")
    assert config_only("direct-bd2", "process_match")


def test_every_hidden_key_is_a_real_option_somewhere():
    declared = {o.key for k in kinds() for o in get(k).options}
    assert EXPECTED <= declared


def test_the_doc_lists_every_config_only_key():
    text = DOC.read_text()
    documented = set(re.findall(r"^\| `([a-z_0-9]+)` \|", text, flags=re.M))
    assert documented == set(CONFIG_ONLY_KEYS) | {"process_match", "exe_name"}


def test_settings_shows_no_editor_for_hidden_options_and_one_for_the_rest(tmp_path, qapp):
    page = SettingsPage()
    try:
        for kind in kinds():
            config = Config.load(tmp_path / f"{kind}.json")
            config.add_profile("sample", backend=kind)
            page.load(config, "sample")
            for option in get(kind).options:
                widget = page.option_widget(option.key)
                if config_only(kind, option.key):
                    assert widget is None, f"{kind}:{option.key} should be hidden"
                else:
                    assert widget is not None, f"{kind}:{option.key} should be shown"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_saving_keeps_hidden_settings_already_in_the_profile(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    hidden = {
        "installer_url": "https://example.test/setup.exe",
        "installer_sha256": "a" * 64,
        "installer_args": "/S",
        "process_match": "game.exe",
    }
    config.add_profile("sample", backend="launcher-nikke", **hidden)
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.save()
        profile = config.profile("sample")
        for key, value in hidden.items():
            assert profile.get(key) == value, key
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_game_executable_is_config_only_except_for_wine_and_script_games():
    for kind in ("direct-bd2", "arknights-yostar", "launcher-nikke", "stellasora-yostar"):
        assert config_only(kind, "exe_name")
    for kind in ("wine", "script"):
        assert not config_only(kind, "exe_name")
