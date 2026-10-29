# SPDX-License-Identifier: GPL-3.0-or-later
"""``nadare remove --delete-backups`` deletes only the recorded rebuild backups."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare import app
from nadare.core import paths
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path):
    # tests/conftest.py points HOME and the XDG dirs at this tmp_path.
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    return cfg, cfg.path, tmp_path


def _folder(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "stale.dll").write_text("old")
    return path


def _backup_setup(env):
    """A live prefix away from the wrapper, one deletable backup, two that must be kept."""
    cfg, cfg_path, tmp = env
    live = _folder(tmp / "Prefixes" / "prefix")
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    valid = _folder(game_dir / "prefix.broken-20260102-030405")
    bad_name = _folder(tmp / "Elsewhere" / "old-prefix")
    in_nadare = _folder(paths.profiles_dir() / "prefix.broken-20260103-030405")
    cfg.profiles["gone"].update(
        prefix=str(live),
        prefix_backups=[str(valid), str(bad_name), str(in_nadare)],
        owned_dirs=[str(game_dir)],
    )
    cfg.save()
    return cfg_path, live, game_dir, valid, bad_name, in_nadare


def test_without_the_flag_recorded_backups_are_untouched(env, capsys):
    cfg_path, live, game_dir, valid, bad_name, in_nadare = _backup_setup(env)

    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes"])

    assert code == 0
    assert "gone" not in Config.load(cfg_path).profiles
    assert live.is_dir() and game_dir.is_dir()
    assert valid.is_dir() and bad_name.is_dir() and in_nadare.is_dir()


def test_delete_backups_deletes_only_the_safe_recorded_backup(env, capsys):
    cfg_path, live, game_dir, valid, bad_name, in_nadare = _backup_setup(env)

    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-backups"])
    out = capsys.readouterr().out

    assert code == 0
    assert "removed gone" in out
    assert not valid.exists()
    assert f"deleted: {valid}" in out
    # The live prefix was not requested, so it and its files stay.
    assert live.is_dir() and (live / "stale.dll").read_text() == "old"
    # The odd ones are kept and each is reported with its reason.
    assert bad_name.is_dir()
    assert "is not named like an old prefix backup" in out
    assert str(bad_name) in out
    assert in_nadare.is_dir()
    assert "inside nadare's own directories" in out
    assert str(in_nadare) in out
    # The now-empty game folder and its Nadare parent are tidied.
    assert not game_dir.exists()
    assert not (game_dir.parent).exists()
    assert "gone" not in Config.load(cfg_path).profiles


def test_delete_backups_needs_the_existing_yes_confirmation(env, capsys):
    cfg_path, live, game_dir, valid, bad_name, in_nadare = _backup_setup(env)

    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--delete-backups"])

    err = capsys.readouterr().err
    assert code != 0
    assert "re-run with --yes" in err
    assert valid.is_dir() and bad_name.is_dir() and in_nadare.is_dir()
    assert "gone" in Config.load(cfg_path).profiles


def test_delete_backups_works_together_with_delete_prefix(env, capsys):
    cfg, cfg_path, tmp = env
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    prefix = _folder(game_dir / "prefix")
    valid = _folder(game_dir / "prefix.broken-20260102-030405")
    cfg.profiles["gone"].update(
        prefix=str(prefix),
        prefix_backups=[str(valid)],
        owned_trees=["prefix"],
        owned_dirs=[str(game_dir)],
    )
    cfg.save()

    code = app.run_cli(
        [
            "-c",
            str(cfg_path),
            "remove",
            "-p",
            "gone",
            "--yes",
            "--delete-prefix",
            "--delete-backups",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "removed gone" in out
    assert not prefix.exists() and not valid.exists()
    assert not game_dir.exists() and not game_dir.parent.exists()
    assert "gone" not in Config.load(cfg_path).profiles


def test_remove_help_documents_delete_backups(capsys):
    with pytest.raises(SystemExit) as exc:
        app.run_cli(["remove", "--help"])

    assert exc.value.code == 0
    assert "--delete-backups" in capsys.readouterr().out
