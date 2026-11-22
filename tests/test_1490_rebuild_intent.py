# SPDX-License-Identifier: GPL-3.0-or-later
"""A prefix rebuild records itself before the old prefix moves (#1490).

The old prefix was renamed to ``<prefix>.broken-*`` first and recorded only after the whole
rebuild succeeded: a killed process left the game with no prefix and the only backup
unrecorded, and "Delete Nadare data" then deleted it as profile data. Synthetic prefixes only;
the backends are the real Arknights and CZN ones with their runner/process checks stubbed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import libraries, paths, prefixmove, process, removal
from nadare.core.config import Config

SRC = Path(prefixmove.__file__).resolve().parents[2]
KINDS = ["arknights-yostar", "stove-czn"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key, leaf in (("HOME", "home"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                      ("XDG_CONFIG_HOME", "config")):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    monkeypatch.delenv("NADARE_CHANNEL", raising=False)


def _profile(tmp_path, kind, *, default_layout=False):
    cfg = Config.load(tmp_path / "config.json")
    if default_layout:
        prefix = paths.profiles_dir() / "synthetic" / "prefix"
    else:
        prefix = tmp_path / "prefix"
    if kind == "stove-czn":
        from nadare.backends.czn.backend import GAME_SUBDIR

        game = prefix / GAME_SUBDIR
    else:
        game = prefix / "drive_c" / "SyntheticGame"
    game.mkdir(parents=True)
    (game / "save.dat").write_text("synthetic progress")
    (prefix / "system.reg").write_text("old registry")
    cfg.add_profile("synthetic", backend=kind, prefix=str(prefix), install_dir=str(game))
    cfg.save()
    return cfg, prefix, game


_CHILD = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nadare.core import prefixmove
from nadare.core.backends import BackendContext
from nadare.core.config import Config
cfg = Config.load(sys.argv[2])
kind, when = sys.argv[3], sys.argv[4]
ctx = BackendContext('synthetic', cfg.profiles['synthetic'], cfg)
if kind == 'stove-czn':
    from nadare.backends.czn.backend import CznBackend
    backend = CznBackend(ctx)
    backend._running = lambda: False
    backend.install = lambda *a, **k: os._exit(73) if when == 'restored' else None
else:
    from nadare.backends.arknights import ArknightsBackend
    from nadare.backends import winegame
    winegame.scan_pids = lambda *_: []
    backend = ArknightsBackend(ctx)
    backend._running_elsewhere = lambda: False
    backend._ensure_default_runner = lambda **_: None
    backend._run_vcredist = lambda **_: None
    if when == 'restored':
        real = prefixmove.restore_inside
        def restore_then_die(*a, **k):
            real(*a, **k)
            os._exit(73)
        prefixmove.restore_inside = restore_then_die

def ensure():
    if when == 'ensure':
        os._exit(73)
    prefix = backend.prefix()
    (prefix / 'drive_c').mkdir(parents=True, exist_ok=True)
    (prefix / 'system.reg').write_text('fresh registry')
    if when == 'fresh':
        os._exit(73)
    return True
backend._ensure_prefix = ensure
backend.remake_prefix()
os._exit(0)
"""


def _killed_rebuild(cfg, kind, when):
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(SRC), str(cfg.path), kind, when],
        capture_output=True, text=True, timeout=60, env=os.environ,
    )
    assert result.returncode == 73, result.stderr


def _backups(prefix: Path) -> list[Path]:
    return sorted(prefix.parent.glob(f"{prefix.name}.broken-*"))


@pytest.mark.parametrize("when", ["ensure", "fresh", "restored"])
@pytest.mark.parametrize("kind", KINDS)
def test_a_killed_rebuild_is_recorded_and_put_back_at_the_next_start(tmp_path, kind, when):
    cfg, prefix, game = _profile(tmp_path, kind)
    _killed_rebuild(cfg, kind, when)
    backups = _backups(prefix)
    assert len(backups) == 1
    again = Config.load(cfg.path)
    profile = again.profiles["synthetic"]
    assert str(backups[0].resolve()) in profile.get("prefix_backups", []), (
        "the only copy of the old prefix was not recorded")
    assert profile.get(prefixmove.REBUILD_KEY), "no record of the unfinished rebuild"

    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes and "put back" in notes[0], notes
    assert (game / "save.dat").read_text() == "synthetic progress"
    assert (prefix / "system.reg").read_text() == "old registry"
    assert not _backups(prefix)
    profile = again.profiles["synthetic"]
    assert prefixmove.REBUILD_KEY not in profile
    assert not profile.get("prefix_backups")


@pytest.mark.parametrize("kind", KINDS)
def test_remove_data_keeps_the_backup_of_a_killed_rebuild(tmp_path, monkeypatch, kind):
    cfg, prefix, game = _profile(tmp_path, kind, default_layout=True)
    _killed_rebuild(cfg, kind, "ensure")
    backup = _backups(prefix)[0]
    again = Config.load(cfg.path)
    monkeypatch.setattr(removal.backends, "create", lambda *_: SimpleNamespace(
        status=lambda: SimpleNamespace(state="not_installed")))
    result = removal.remove_game(again, "synthetic", delete_data=True, delete_trees=[],
                                 delete_backups=False, applications_dir=tmp_path / "apps",
                                 desktop_dir=tmp_path / "desktop")
    assert not result.refused
    saved = backup / game.relative_to(prefix) / "save.dat"
    assert saved.read_text() == "synthetic progress"


def test_remove_data_keeps_an_unrecorded_old_rebuild_backup(tmp_path, monkeypatch):
    """A backup an older version left unrecorded is kept too (kept, never adopted)."""
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar", default_layout=True)
    backup = prefixmove.move_aside(prefix)
    monkeypatch.setattr(removal.backends, "create", lambda *_: SimpleNamespace(
        status=lambda: SimpleNamespace(state="not_installed")))
    result = removal.remove_game(cfg, "synthetic", delete_data=True, delete_trees=[],
                                 delete_backups=False, applications_dir=tmp_path / "apps",
                                 desktop_dir=tmp_path / "desktop")
    assert not result.refused
    assert (backup / "drive_c/SyntheticGame/save.dat").is_file()
    assert "prefix_backups" not in Config.load(cfg.path).profiles.get("synthetic", {})


def _backend(cfg, kind, monkeypatch):
    from nadare.core.backends import BackendContext

    ctx = BackendContext("synthetic", cfg.profiles["synthetic"], cfg)
    if kind == "stove-czn":
        from nadare.backends.czn.backend import CznBackend

        backend = CznBackend(ctx)
        backend._running = lambda: False
        backend.install = lambda *a, **k: None
    else:
        from nadare.backends import winegame
        from nadare.backends.arknights import ArknightsBackend

        monkeypatch.setattr(winegame, "scan_pids", lambda *_: [])
        backend = ArknightsBackend(ctx)
        backend._running_elsewhere = lambda: False
        backend._ensure_default_runner = lambda **_: None
        backend._run_vcredist = lambda **_: None
    return backend


@pytest.mark.parametrize("kind", KINDS)
def test_a_finished_rebuild_keeps_its_backup_recorded_and_no_intent(tmp_path, monkeypatch, kind):
    cfg, prefix, game = _profile(tmp_path, kind)
    backend = _backend(cfg, kind, monkeypatch)

    def ensure():
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        return True

    backend._ensure_prefix = ensure
    backend.remake_prefix()
    saved = Config.load(cfg.path).profiles["synthetic"]
    backup = _backups(prefix)[0]
    assert saved["prefix_backups"] == [str(backup.resolve())]
    assert prefixmove.REBUILD_KEY not in saved
    assert (game / "save.dat").read_text() == "synthetic progress"


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_rebuild_rolls_back_and_forgets_its_record(tmp_path, monkeypatch, kind):
    cfg, prefix, game = _profile(tmp_path, kind)
    backend = _backend(cfg, kind, monkeypatch)

    def ensure():
        raise OSError("wineboot failed")

    backend._ensure_prefix = ensure
    with pytest.raises(OSError, match="wineboot failed"):
        backend.remake_prefix()
    saved = Config.load(cfg.path).profiles["synthetic"]
    assert not saved.get("prefix_backups")
    assert prefixmove.REBUILD_KEY not in saved
    assert (prefix / "system.reg").read_text() == "old registry"
    assert not _backups(prefix)


def test_recovery_leaves_a_busy_profile_alone(tmp_path):
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar")
    _killed_rebuild(cfg, "arknights-yostar", "ensure")
    again = Config.load(cfg.path)
    with process.OperationLock("synthetic"):
        assert prefixmove.finish_interrupted_rebuilds(again) == []
    assert again.profiles["synthetic"].get(prefixmove.REBUILD_KEY)
    assert _backups(prefix)


def test_recovery_never_adopts_a_replaced_backup(tmp_path):
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar")
    _killed_rebuild(cfg, "arknights-yostar", "ensure")
    backup = _backups(prefix)[0]
    aside = tmp_path / "aside"
    backup.rename(aside)
    backup.mkdir()
    (backup / "other.txt").write_text("not the old prefix")
    again = Config.load(cfg.path)
    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes and "kept" in notes[0], notes
    assert again.profiles["synthetic"].get(prefixmove.REBUILD_KEY)
    assert (backup / "other.txt").exists()
    assert (aside / "drive_c/SyntheticGame/save.dat").exists()
    assert not prefix.exists()


def test_a_record_whose_rename_never_happened_is_just_forgotten(tmp_path):
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar")
    info = os.lstat(prefix)
    backup = prefixmove.backup_path(prefix)
    profile = cfg.profiles["synthetic"]
    profile["prefix_backups"] = [str(backup.resolve())]
    profile[prefixmove.REBUILD_KEY] = {
        "prefix": str(prefix), "backup": str(backup.resolve()),
        "id": [info.st_dev, info.st_ino], "keep": [],
    }
    cfg.save()
    again = Config.load(cfg.path)
    notes = prefixmove.finish_interrupted_rebuilds(again)
    assert notes, notes
    assert prefixmove.REBUILD_KEY not in again.profiles["synthetic"]
    assert not again.profiles["synthetic"].get("prefix_backups")
    assert (prefix / "system.reg").read_text() == "old registry"


def test_the_start_up_upkeep_puts_it_back(tmp_path):
    cfg, prefix, game = _profile(tmp_path, "stove-czn")
    _killed_rebuild(cfg, "stove-czn", "restored")
    again = Config.load(cfg.path)
    assert libraries.backfill(again) is True
    assert (game / "save.dat").read_text() == "synthetic progress"
    assert prefixmove.REBUILD_KEY not in again.profiles["synthetic"]


def test_a_rename_that_fails_forgets_its_record(tmp_path, monkeypatch):
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar")
    backend = _backend(cfg, "arknights-yostar", monkeypatch)

    def no_rename(*_args):
        raise OSError("read-only")

    monkeypatch.setattr(prefixmove.os, "rename", no_rename)
    with pytest.raises(prefixmove.PrefixMoveError):
        backend.remake_prefix()
    saved = Config.load(cfg.path).profiles["synthetic"]
    assert prefixmove.REBUILD_KEY not in saved
    assert not saved.get("prefix_backups")


def test_a_record_pointing_outside_the_prefix_folder_is_refused(tmp_path):
    cfg, prefix, _ = _profile(tmp_path, "arknights-yostar")
    elsewhere = tmp_path / "elsewhere" / "prefix.broken-x"
    elsewhere.mkdir(parents=True)
    info = os.lstat(elsewhere)
    prefix.rename(tmp_path / "moved-away")
    cfg.profiles["synthetic"][prefixmove.REBUILD_KEY] = {
        "prefix": str(prefix), "backup": str(elsewhere),
        "id": [info.st_dev, info.st_ino], "keep": [],
    }
    notes = prefixmove.finish_interrupted_rebuilds(cfg)
    assert notes and "kept" in notes[0], notes
    assert elsewhere.is_dir() and not prefix.exists()
