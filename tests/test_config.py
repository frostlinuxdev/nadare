# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.config."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core.config import PROFILE_DEFAULTS, TOP_DEFAULTS, Config, valid_profile_name


def test_load_missing_file_yields_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config.load(path)

    assert not path.exists()
    assert cfg.path == path
    for key, value in TOP_DEFAULTS.items():
        assert cfg.data[key] == value
    assert cfg.profiles == {}
    assert cfg.active == ""


def test_normalise_merges_profile_defaults_and_keeps_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "future_key": {"nested": True},
                "profiles": {"game": {"title": "Game"}},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(path)

    profile = cfg.profile("game")
    assert profile["title"] == "Game"
    # Unspecified per-profile keys fall back to defaults.
    for key, value in PROFILE_DEFAULTS.items():
        if key != "title":
            assert profile[key] == value
    # Unknown top-level keys are preserved for forward compatibility.
    assert cfg.data["future_key"] == {"nested": True}


def test_add_profile_and_active_selection(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    first = cfg.add_profile("alpha", title="Alpha", extra={"x": 1})
    second = cfg.add_profile("beta")

    assert first["title"] == "Alpha"
    assert first["extra"] == {"x": 1}
    assert first["backend"] == ""  # defaults applied
    assert second["backend"] == ""
    # The first profile added becomes active; later ones do not steal focus.
    assert cfg.active == "alpha"

    cfg.set_active("beta")
    assert cfg.active == "beta"
    assert cfg.profile() is second
    assert cfg.profile("alpha") is first

    with pytest.raises(KeyError):
        cfg.set_active("gamma")
    with pytest.raises(KeyError):
        cfg.profile("gamma")


def test_remove_profile_via_dict_and_note_active(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("alpha")
    cfg.add_profile("beta")
    cfg.set_active("beta")

    del cfg.profiles["alpha"]
    assert "alpha" not in cfg.profiles
    assert cfg.active == "beta"

    # Removing the active profile is done through the mapping; config does not
    # re-point ``active_profile``.
    del cfg.profiles["beta"]
    assert cfg.profiles == {}


def test_remove_profile_public_api(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("alpha")
    cfg.remove_profile("alpha")  # type: ignore[attr-defined]


def test_save_is_atomic_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("game", title="Reloaded")
    cfg.data["proxy_url"] = "http://127.0.0.1:9"

    saved = cfg.save()

    assert saved == path
    assert path.is_file()
    # No stray temp files from the atomic write.
    assert list(path.parent.glob(".config-*")) == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["proxy_url"] == "http://127.0.0.1:9"

    reloaded = Config.load(path)
    assert reloaded.data == cfg.data
    assert reloaded.profile("game")["title"] == "Reloaded"
    assert reloaded.active == "game"


def test_malformed_config_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ this is not valid json", encoding="utf-8")

    cfg = Config.load(path)

    assert cfg.profiles == {}
    assert cfg.active == ""
    assert cfg.data["schema"] == TOP_DEFAULTS["schema"]
    # The bad file is quarantined so a later atomic save cannot destroy it.
    assert not path.exists()
    saved = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8").startswith("{ this")


def test_non_object_config_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.profiles == {}


def test_malformed_config_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ broken", encoding="utf-8")

    Config.load(path)

    leftovers = (
        list(tmp_path.glob("config.json.*"))
        + list(tmp_path.glob("*.corrupt*"))
        + list(tmp_path.glob("*.bak"))
    )
    assert leftovers, "expected a quarantine/backup of the malformed config"


# --------------------------------------------------------------------------
# profile-name validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "game",
        "My Game",
        "game-2",
        "a_b.c",
        "Alpha1",
        "x" * 64,
        "Nino's game",
        "Café",
        "ゲーム",
        "Fate Grand Order (NA)",
    ],
)
def test_valid_profile_name_accepts_safe_names(name: str) -> None:
    assert valid_profile_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        ".",
        "..",
        "/etc/passwd",
        "../escape",
        "..\\escape",
        "a/b",
        "a\\b",
        "C:evil",
        "x" * 65,
        "a\x00b",
        "sneaky\nnewline",
        "Fate/Grand Order (NA)",
    ],
)
def test_valid_profile_name_rejects_unsafe_names(name: str) -> None:
    assert not valid_profile_name(name)


def test_valid_profile_name_rejects_non_strings() -> None:
    assert not valid_profile_name(None)
    assert not valid_profile_name(1)


def test_add_profile_rejects_unsafe_name(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    with pytest.raises(ValueError):
        cfg.add_profile("../escape", backend="wine")
    assert cfg.profiles == {}


def test_add_profile_accepts_unicode_name(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("ゲーム", backend="wine", prefix="/p")
    assert cfg.profile("ゲーム")["prefix"] == "/p"


@pytest.mark.parametrize("name", ["Nino's game", "Café", "ゲーム", "Fate Grand Order (NA)"])
def test_unicode_profile_names_survive_load_and_save(tmp_path: Path, name: str) -> None:
    """Regression (N2): a legal non-ASCII/punctuation name must not be dropped
    on load, and the next save must keep it."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {name: {"backend": "wine", "prefix": "/p", "runner": "/r"}},
                "active_profile": name,
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)
    assert set(cfg.profiles) == {name}
    assert cfg.profile(name)["prefix"] == "/p"
    assert cfg.active == name

    cfg.save()
    reloaded = Config.load(path)
    assert set(reloaded.profiles) == {name}
    assert reloaded.profile(name)["runner"] == "/r"


def test_load_migrates_profiles_with_unsafe_names_without_loss(tmp_path: Path) -> None:
    """Square brackets: an unsafe key is migrated (not silently discarded), and
    its install_dir/prefix/runner survive."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "Fate/Grand Order (NA)": {
                        "backend": "wine",
                        "install_dir": "/games/fgo",
                        "prefix": "/prefixes/fgo",
                    },
                    "good": {"backend": "wine", "title": "Good"},
                },
                "active_profile": "good",
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    assert "Fate/Grand Order (NA)" not in cfg.profiles
    assert "good" in cfg.profiles
    assert len(cfg.profiles) == 2
    migrated = cfg.profiles["Fate-Grand Order (NA)"]
    assert migrated["install_dir"] == "/games/fgo"
    assert migrated["prefix"] == "/prefixes/fgo"
    assert cfg.active == "good"


def test_load_migrates_traversal_keys_and_avoids_collisions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "../evil": {"backend": "wine", "prefix": "/p"},
                    "..-evil": {"backend": "wine", "title": "Legit"},
                    "a/b": {"backend": "wine"},
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    assert "../evil" not in cfg.profiles
    assert "a/b" not in cfg.profiles
    # The migrated ``../evil`` cannot clobber the legitimately named ``..-evil``.
    assert cfg.profiles["..-evil"]["title"] == "Legit"
    assert len(cfg.profiles) == 3
    assert all(valid_profile_name(key) for key in cfg.profiles)


def test_load_migrates_drive_letter_key_to_a_valid_name(tmp_path: Path) -> None:
    """R1: a migrated key must itself pass valid_profile_name (drive letter)."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"profiles": {"C:evil": {"backend": "wine", "prefix": "/p"}}}),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    assert all(valid_profile_name(key) for key in cfg.profiles)
    assert list(cfg.profiles) == ["evil"]
    assert cfg.profiles["evil"]["prefix"] == "/p"


def test_migration_maps_the_active_profile(tmp_path: Path) -> None:
    """R2: renaming the active profile must not silently pick another one."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "Fate/Grand Order (NA)": {"backend": "wine", "prefix": "/fgo"},
                    "good": {"backend": "wine", "prefix": "/good"},
                },
                "active_profile": "Fate/Grand Order (NA)",
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    assert cfg.active == "Fate-Grand Order (NA)"
    assert cfg.profile()["prefix"] == "/fgo"


def test_quarantine_names_do_not_collide(tmp_path: Path) -> None:
    """R5: two corrupt configs must both be preserved, not overwrite each other."""
    path = tmp_path / "config.json"
    path.write_text("{ broken one", encoding="utf-8")
    Config.load(path)
    path.write_text("{ broken two", encoding="utf-8")
    Config.load(path)

    saved = sorted(tmp_path.glob("config.json.corrupt-*"))
    assert len(saved) == 2


def test_save_fsyncs_file_and_directory(tmp_path: Path, monkeypatch) -> None:
    """N4: the atomic rename must be durable (file + parent dir fsync)."""
    from nadare.core import config as config_mod

    fsyncs: list[int] = []
    real_fsync = os.fsync

    def fake_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(config_mod.os, "fsync", fake_fsync)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("game")
    cfg.save()
    # One fsync for the file contents and one for the directory entry.
    assert len(fsyncs) >= 2


def test_proxy_url_is_no_longer_a_default(tmp_path: Path) -> None:
    """L7: the dead ``proxy_url`` key is no longer materialised."""
    assert "proxy_url" not in TOP_DEFAULTS
    cfg = Config.load(tmp_path / "config.json")
    assert "proxy_url" not in cfg.data
    cfg.save()
    assert "proxy_url" not in json.loads(cfg.path.read_text(encoding="utf-8"))


def test_legacy_proxy_url_is_preserved_on_load(tmp_path: Path) -> None:
    """L7: dropping the default must not destroy a user's stored value."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"proxy_url": "http://127.0.0.1:9", "profiles": {}}),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    assert cfg.data["proxy_url"] == "http://127.0.0.1:9"
    cfg.save()
    assert json.loads(path.read_text(encoding="utf-8"))["proxy_url"] == "http://127.0.0.1:9"



def test_config_wrong_scalar_types_do_not_crash(tmp_path):
    import json

    from nadare import app

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"active_profile": [], "language": ["en"], "profiles": {"a": {}, "b": 5}})
    )
    cfg = Config.load(p)
    assert cfg.data["active_profile"] == "a"
    assert set(cfg.profiles) == {"a", "b"}
    assert app.run_cli(["-c", str(p), "list"]) == 0


def test_unreadable_config_raises_and_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    from nadare.core.config import ConfigReadError

    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    real = Path.read_text

    def deny(self, *a, **k):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(ConfigReadError, match="cannot read"):
        Config.load(path)
    assert path.exists()
    assert not list(tmp_path.glob("*.corrupt*"))


def test_cli_reports_unreadable_config(tmp_path: Path, monkeypatch, capsys) -> None:
    from nadare import app
    from nadare.core.config import ConfigReadError

    def boom(p=None):
        raise ConfigReadError("cannot read /x: Permission denied")

    monkeypatch.setattr(app.Config, "load", staticmethod(boom))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "k"))
    assert app.run_cli(["list"]) == 1
    assert "cannot read /x" in capsys.readouterr().err


def test_malformed_config_sets_notice_with_backup_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ nope", encoding="utf-8")
    cfg = Config.load(path)
    backup = next(tmp_path.glob("config.json.corrupt-*"))
    assert cfg.notice and str(backup) in cfg.notice


def test_save_keeps_profiles_added_by_another_process(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    a = Config.load(path)
    a.add_profile("one")
    a.save()
    b = Config.load(path)
    other = Config.load(path)
    other.add_profile("two")
    other.save()
    b.add_profile("three")
    b.save()
    assert set(Config.load(path).profiles) == {"one", "two", "three"}


def test_save_does_not_resurrect_removed_profile(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    a = Config.load(path)
    a.add_profile("one")
    a.add_profile("two")
    a.save()
    a.remove_profile("one")
    a.save()
    assert set(Config.load(path).profiles) == {"two"}


def test_save_takes_disk_edit_of_untouched_profile(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    a = Config.load(path)
    a.add_profile("one", title="old")
    a.add_profile("two")
    a.save()
    b = Config.load(path)
    other = Config.load(path)
    other.profiles["one"]["title"] = "new"
    other.save()
    b.profiles["two"]["title"] = "mine"
    b.save()
    final = Config.load(path)
    assert final.profiles["one"]["title"] == "new"
    assert final.profiles["two"]["title"] == "mine"
