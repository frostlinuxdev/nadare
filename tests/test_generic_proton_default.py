# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic "Wine / Proton game" profiles run on Proton by default (maintainer, 2026-11-13).

"Only Proton, or a custom Wine they select." No game runs on an auto-detected system Wine
any more. Profiles that already ran on plain Wine keep it: a one-time migration at load
writes the Wine they used into ``wine_binary`` (an explicit choice from then on).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import wine as wine_mod
from nadare.backends.wine import WineBackend
from nadare.core import legacywine, runners, umu
from nadare.core.config import Config


@pytest.fixture
def default_runner(tmp_path, monkeypatch):
    pinned = tmp_path / "runners" / "dwproton-11.0-15"
    (pinned).mkdir(parents=True)
    (pinned / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runners, "installed_pinned", lambda family=runners.DEFAULT_FAMILY: pinned)
    monkeypatch.setattr(
        runners, "ensure_pinned", lambda family=runners.DEFAULT_FAMILY, **kw: pinned
    )
    monkeypatch.setattr(umu, "system", lambda configured=None: "/usr/bin/umu-run")
    return pinned


def _ctx(tmp_path, **extra):
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    values = {"install_dir": str(game), "exe_name": "game.exe", "prefix": str(tmp_path / "pfx")}
    values.update(extra)
    return make_context(tmp_path, "wine", values)


def test_a_new_generic_game_runs_on_the_default_proton(tmp_path, monkeypatch, default_runner):
    monkeypatch.setattr(wine_mod, "first_wine", lambda: pytest.fail("no system Wine lookup"))
    argv, env, *_ = WineBackend(_ctx(tmp_path)).build()
    assert argv[0] == "/usr/bin/umu-run", argv
    assert env["PROTONPATH"] == str(default_runner)


def test_a_chosen_custom_wine_is_still_used(tmp_path, default_runner):
    argv, *_ = WineBackend(_ctx(tmp_path, wine_binary="/opt/my-wine/bin/wine")).build()
    assert argv[0] == "/opt/my-wine/bin/wine"


def test_status_never_downloads_the_default_runner(tmp_path, monkeypatch, default_runner):
    monkeypatch.setattr(runners, "installed_pinned", lambda family=runners.DEFAULT_FAMILY: None)
    monkeypatch.setattr(
        runners, "ensure_pinned", lambda *a, **k: pytest.fail("status must not download")
    )
    status = WineBackend(_ctx(tmp_path)).status()
    assert status.state != "error", status.message  # fetched at the first launch


def _legacy_config(tmp_path, *, prefix_exists: bool, **profile) -> Path:
    prefix = tmp_path / "pfx"
    if prefix_exists:
        (prefix / "drive_c").mkdir(parents=True)
    data = {"profiles": {"mine": {"backend": "wine", "title": "Mine", "prefix": str(prefix),
                                  "install_dir": str(tmp_path / "game"), **profile}}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_an_existing_plain_wine_game_keeps_its_wine(tmp_path, monkeypatch):
    monkeypatch.setattr(legacywine, "find_plain_wine", lambda: Path("/usr/bin/wine"))
    cfg = Config.load(_legacy_config(tmp_path, prefix_exists=True))
    assert cfg.profiles["mine"].get("wine_binary") == "/usr/bin/wine"


def test_a_game_never_started_gets_proton(tmp_path, monkeypatch):
    monkeypatch.setattr(legacywine, "find_plain_wine", lambda: Path("/usr/bin/wine"))
    cfg = Config.load(_legacy_config(tmp_path, prefix_exists=False))
    assert not cfg.profiles["mine"].get("wine_binary")


@pytest.mark.parametrize("chosen", [{"runner": "GE-Proton10-1"}, {"wine_binary": "/x/wine"},
                                    {"extra": {"proton_runner": "/r/proton"}}])
def test_a_game_with_a_choice_is_left_alone(tmp_path, monkeypatch, chosen):
    monkeypatch.setattr(legacywine, "find_plain_wine", lambda: Path("/usr/bin/wine"))
    cfg = Config.load(_legacy_config(tmp_path, prefix_exists=True, **chosen))
    profile = cfg.profiles["mine"]
    assert profile.get("wine_binary") in (None, "", "/x/wine"), profile


def test_the_migration_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(legacywine, "find_plain_wine", lambda: Path("/usr/bin/wine"))
    path = _legacy_config(tmp_path, prefix_exists=True)
    cfg = Config.load(path)
    cfg.save()
    cfg = Config.load(path)
    cfg.profiles["mine"].pop("wine_binary")  # the user switches it to Proton
    cfg.save()
    again = Config.load(path)
    assert not again.profiles["mine"].get("wine_binary")


def test_settings_offers_the_default_proton_and_no_system_wine():
    from nadare.gui.settings_parts.extras import SYSTEM_WINE, discover_runners

    entries = discover_runners("wine")
    assert entries and entries[0][0].startswith("Default · recommended"), entries
    assert all(label != SYSTEM_WINE for label, _value in entries), entries
