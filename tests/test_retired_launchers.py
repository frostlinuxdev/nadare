# SPDX-License-Identifier: GPL-3.0-or-later
"""The publisher-launcher duplicates of direct games are retired (user decision 2026-11-10).

Nadare installs these games itself (``direct-nte``, ``direct-gfl2-haoplay``, both Verified).
NIKKE keeps its launcher: its sign-in needs it. A profile made before keeps a plain way out.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import backends, removal, storecatalog
from nadare.core.config import Config

RETIRED = ("launcher-nte", "launcher-gfl2-haoplay")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), tmp_path


def test_the_retired_kinds_are_gone_and_nikke_stays():
    kinds = backends.kinds()
    for kind in RETIRED:
        assert kind not in kinds
    assert "launcher-nikke" in kinds
    assert "direct-nte" in kinds
    assert "direct-gfl2-haoplay" in kinds


@pytest.mark.parametrize(
    "kind, title, direct",
    [
        ("launcher-nte", "Neverness to Everness", "direct-nte"),
        ("launcher-gfl2-haoplay", "Girls' Frontline 2", "direct-gfl2-haoplay"),
    ],
)
def test_a_retired_kind_explains_what_to_do(kind, title, direct):
    assert backends.retired_replacement(kind) == direct
    with pytest.raises(backends.BackendError) as caught:
        backends.get(kind)
    text = str(caught.value)
    assert title in text
    assert "no longer" in text
    assert "Store" in text
    assert "files" in text


def test_other_kinds_are_not_retired():
    assert backends.retired_replacement("launcher-nikke") is None
    assert backends.retired_replacement("direct-nte") is None
    assert backends.retired_replacement("no-such-kind") is None


def test_the_store_and_add_game_no_longer_offer_them():
    from nadare.gui.add_game import _catalog

    offered = {game.kind for game in _catalog()}
    listed = {entry.kind for entry in storecatalog.entries()}
    for kind in RETIRED:
        assert kind not in offered
        assert kind not in listed
    assert "launcher-nikke" in offered
    assert {"direct-nte", "direct-gfl2-haoplay"} <= listed


def test_a_profile_of_a_retired_kind_shows_the_reason(env):
    cfg, _tmp = env
    cfg.add_profile("nte-launcher", backend="launcher-nte", title="Neverness to Everness")
    with pytest.raises(backends.BackendError, match="no longer"):
        backends.create("nte-launcher", cfg)


def test_a_profile_of_a_retired_kind_can_still_be_removed_keeping_files(env):
    cfg, tmp = env
    prefix = tmp / "games" / "NTE" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile(
        "nte-launcher", backend="launcher-nte", title="Neverness to Everness", prefix=str(prefix)
    )
    cfg.save()
    result = removal.remove_game(cfg, "nte-launcher", applications_dir=tmp / "apps")
    assert not result.profile_kept
    assert "nte-launcher" not in Config.load(tmp / "config.json").profiles
    assert (prefix / "drive_c").is_dir()


def test_the_message_says_where_the_old_files_are(env):
    cfg, tmp = env
    prefix = tmp / "games" / "GF2" / "prefix"
    cfg.add_profile(
        "gfl2-hp-launcher",
        backend="launcher-gfl2-haoplay",
        title="Girls' Frontline 2",
        prefix=str(prefix),
    )
    with pytest.raises(backends.BackendError) as caught:
        backends.create("gfl2-hp-launcher", cfg)
    text = str(caught.value)
    assert str(prefix) in text
    assert "delete" in text


def test_a_retired_profile_without_a_prefix_still_gets_the_plain_message(env):
    cfg, _tmp = env
    cfg.add_profile("nte-launcher", backend="launcher-nte", title="Neverness to Everness")
    with pytest.raises(backends.BackendError, match="install Neverness to Everness from"):
        backends.create("nte-launcher", cfg)


def test_the_game_page_shows_the_retired_message_not_unknown_type():
    """Seen in a b23-rc1 click-through: the page header said "type not recognised"."""
    from nadare.gui.main_window import _status_error_text

    retired = "Nadare no longer runs Neverness to Everness through the publisher's launcher …"
    assert _status_error_text("launcher-nte", retired) == retired
    assert "not recognised" in _status_error_text("no-such-kind", "unknown backend: 'x'")
    assert "not recognised" in _status_error_text("", "no backend")
    assert _status_error_text("direct-nte", "the prefix is gone") == "the prefix is gone"


@pytest.mark.parametrize("kind", RETIRED)
def test_removal_refuses_while_a_retired_game_still_runs(env, monkeypatch, kind):
    """b22 -> b23 with the old launcher game still open: Remove must not delete under it.

    ``backends.create`` raises for a retired kind, so the usual status probe cannot answer;
    the prefix's own processes (and the run lock) decide instead (round-1 review, 2026-11-11).
    """
    from nadare.core import process

    cfg, tmp = env
    prefix = tmp / "games" / "old" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile("old-launcher", backend=kind, title="Old", prefix=str(prefix))
    cfg.save()
    asked: list[str] = []

    def fake_scan(where, *a, **k):
        asked.append(str(where))
        return [4242]

    monkeypatch.setattr(process, "prefix_pids", fake_scan)
    result = removal.remove_game(
        cfg, "old-launcher", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )
    assert result.profile_kept
    assert any("running" in w for w in result.warnings)
    assert (prefix / "drive_c").is_dir()
    assert asked and asked[0] == str(prefix)
    assert "old-launcher" in Config.load(tmp / "config.json").profiles


def test_removal_of_a_stopped_retired_game_still_deletes_what_was_ticked(env, monkeypatch):
    from nadare.core import process

    cfg, tmp = env
    prefix = tmp / "games" / "old" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile(
        "old-launcher",
        backend="launcher-nte",
        title="Old",
        prefix=str(prefix),
        owned_trees=["prefix"],
    )
    cfg.save()
    monkeypatch.setattr(process, "prefix_pids", lambda *a, **k: [])
    result = removal.remove_game(
        cfg, "old-launcher", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )
    assert not result.profile_kept
    assert "old-launcher" not in Config.load(tmp / "config.json").profiles


def test_prefix_pids_finds_wine_and_proton_processes_of_that_prefix(tmp_path):
    from nadare.core import process

    prefix = tmp_path / "prefix"
    proc = tmp_path / "proc"

    def fake(pid, env):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"wine\0game.exe\0")
        (d / "environ").write_bytes("".join(f"{k}={v}\0" for k, v in env.items()).encode())

    fake(11, {"WINEPREFIX": str(prefix)})
    fake(12, {"STEAM_COMPAT_DATA_PATH": str(prefix), "WINEPREFIX": str(prefix / "pfx")})
    fake(13, {"WINEPREFIX": str(tmp_path / "other")})
    fake(14, {"HOME": str(tmp_path)})
    assert sorted(process.prefix_pids(prefix, proc_root=proc)) == [11, 12]


def test_removal_finds_a_retired_prefix_kept_under_extra(env, monkeypatch):
    from nadare.core import process

    cfg, tmp = env
    prefix = tmp / "games" / "old" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile(
        "old-launcher", backend="launcher-nte", title="Old", extra={"prefix": str(prefix)}
    )
    cfg.save()
    asked: list[str] = []
    monkeypatch.setattr(
        process, "prefix_pids", lambda where, *a, **k: asked.append(str(where)) or [1]
    )
    result = removal.remove_game(cfg, "old-launcher", applications_dir=tmp / "apps")
    assert result.profile_kept
    assert asked == [str(prefix)]
