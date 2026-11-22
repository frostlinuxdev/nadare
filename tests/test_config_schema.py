# SPDX-License-Identifier: GPL-3.0-or-later
"""#6: the config's ``schema`` field is read, checked and migrated, not just carried along."""

from __future__ import annotations

import json

import pytest

from nadare.core import config as config_mod
from nadare.core.config import SCHEMA, Config


def _write(tmp_path, **top):
    path = tmp_path / "config.json"
    payload = {"active_profile": "a", "profiles": {"a": {"backend": "wine"}}}
    payload.update(top)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _copies(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if ".corrupt-" in p.name)


def test_the_current_schema_loads_quietly(tmp_path):
    cfg = Config.load(_write(tmp_path, schema=SCHEMA))
    assert cfg.data["schema"] == SCHEMA
    assert cfg.notice is None
    assert _copies(tmp_path) == []


@pytest.mark.parametrize("value", [None, "1", "x", True, 0, -3, 1.5, [1]])
def test_a_missing_or_malformed_schema_counts_as_the_current_one(tmp_path, value):
    top = {} if value is None else {"schema": value}
    cfg = Config.load(_write(tmp_path, **top))
    assert cfg.data["schema"] == SCHEMA
    assert "a" in cfg.profiles
    assert cfg.notice is None


def test_a_newer_schema_keeps_a_copy_and_says_so(tmp_path):
    path = _write(tmp_path, schema=SCHEMA + 1)
    cfg = Config.load(path)
    assert "a" in cfg.profiles  # still usable, best effort
    copies = _copies(tmp_path)
    assert len(copies) == 1
    assert cfg.notice and "newer" in cfg.notice and copies[0] in cfg.notice
    # The copy is the untouched original.
    assert json.loads((tmp_path / copies[0]).read_text())["schema"] == SCHEMA + 1


def test_saving_does_not_downgrade_a_newer_schema_marker(tmp_path):
    path = _write(tmp_path, schema=SCHEMA + 1)
    cfg = Config.load(path)
    cfg.data["check_updates"] = False
    cfg.save()
    assert json.loads(path.read_text())["schema"] == SCHEMA + 1


def test_older_schemas_run_the_migrations_in_order(tmp_path, monkeypatch):
    seen: list[int] = []

    def step(n):
        def run(raw):
            seen.append(n)
            raw.setdefault("profiles", {})["a"]["title"] = f"after {n}"
        return run

    monkeypatch.setattr(config_mod, "SCHEMA", 3)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: step(1), 2: step(2)})
    cfg = Config.load(_write(tmp_path, schema=1))
    assert seen == [1, 2]
    assert cfg.profiles["a"]["title"] == "after 2"
    assert cfg.data["schema"] == 3


def test_a_failing_migration_keeps_a_copy_and_loads_what_it_can(tmp_path, monkeypatch):
    def broken(raw):
        raise KeyError("boom")

    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: broken})
    cfg = Config.load(_write(tmp_path, schema=1))
    assert "a" in cfg.profiles
    assert len(_copies(tmp_path)) == 1
    assert cfg.notice and "could not be updated" in cfg.notice


def test_a_migrated_config_is_written_back_on_the_next_save(tmp_path, monkeypatch):
    def rename_title(raw):
        raw["profiles"]["a"]["title"] = "migrated"
        raw["check_notices"] = False

    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: rename_title})
    path = _write(tmp_path, schema=1)
    Config.load(path).save()
    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == 2
    assert on_disk["profiles"]["a"]["title"] == "migrated"
    assert on_disk["check_notices"] is False


# --- #1476: a failed step is not stamped as done ---------------------------------------------


def _half_then_raise(raw):
    raw["profiles"]["a"]["title"] = "half-migrated"
    raw["half"] = True
    raise KeyError("boom")


def test_a_failed_migration_is_not_persisted_as_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: _half_then_raise})
    path = _write(tmp_path, schema=1)
    cfg = Config.load(path)
    assert cfg.notice and "could not be updated" in cfg.notice
    cfg.save()
    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == 1, "the failed step was stamped as done"
    assert "half" not in on_disk, "the failed step's partial edits were kept"
    assert on_disk["profiles"]["a"].get("title") != "half-migrated"


def test_a_later_build_with_a_fixed_step_runs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: _half_then_raise})
    path = _write(tmp_path, schema=1)
    Config.load(path).save()

    def fixed(raw):
        raw["profiles"]["a"]["title"] = "migrated"

    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: fixed})
    cfg = Config.load(path)
    assert cfg.notice is None
    assert cfg.profiles["a"]["title"] == "migrated"
    cfg.save()
    assert json.loads(path.read_text())["schema"] == 2


def test_steps_before_the_failing_one_are_kept_and_stamped(tmp_path, monkeypatch):
    def one(raw):
        raw["profiles"]["a"]["title"] = "after 1"

    monkeypatch.setattr(config_mod, "SCHEMA", 3)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: one, 2: _half_then_raise})
    path = _write(tmp_path, schema=1)
    cfg = Config.load(path)
    assert cfg.profiles["a"]["title"] == "after 1"
    assert cfg.data["schema"] == 2
    cfg.save()
    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == 2
    assert on_disk["profiles"]["a"]["title"] == "after 1"
    assert "half" not in on_disk


def test_a_failed_step_with_no_backup_is_still_not_stamped(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: _half_then_raise})
    monkeypatch.setattr(Config, "_quarantine", classmethod(lambda cls, p, keep=False: None))
    path = _write(tmp_path, schema=1)
    cfg = Config.load(path)
    assert "could not be backed up" in (cfg.notice or "")
    cfg.save()
    assert json.loads(path.read_text())["schema"] == 1


def test_a_failed_migration_still_loads_the_library(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: _half_then_raise})
    cfg = Config.load(_write(tmp_path, schema=1))
    assert "a" in cfg.profiles and cfg.profiles["a"]["backend"] == "wine"
