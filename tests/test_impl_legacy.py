# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for upgrading configs written by older Nadare betas."""

from __future__ import annotations

import json
from pathlib import Path

from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core import paths
from nadare.core.config import Config


def _write_config(tmp_path: Path, profiles: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return path


def test_legacy_runner_and_prefix_keys_become_canonical(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "sample": {
                "backend": "wine",
                "proton_runner": "/old/runner",
                "wineprefix": "/old/prefix",
            }
        },
    )

    cfg = Config.load(path)

    profile = cfg.profile("sample")
    assert profile["runner"] == "/old/runner"
    assert profile["prefix"] == "/old/prefix"
    assert "proton_runner" not in profile
    assert "wineprefix" not in profile


def test_canonical_runner_and_prefix_win_over_legacy_keys(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "sample": {
                "backend": "wine",
                "proton_runner": "/old/runner",
                "runner": "/new/runner",
                "wineprefix": "/old/prefix",
                "prefix": "/new/prefix",
            }
        },
    )

    cfg = Config.load(path)

    profile = cfg.profile("sample")
    assert profile["runner"] == "/new/runner"
    assert profile["prefix"] == "/new/prefix"
    assert "proton_runner" not in profile
    assert "wineprefix" not in profile


def test_unsafe_profile_rename_moves_the_default_data_folder(tmp_path: Path) -> None:
    old = "Fate/Grand Order (NA)"
    new = "Fate-Grand Order (NA)"
    old_dir = paths.profiles_dir() / old
    (old_dir / "install").mkdir(parents=True)
    (old_dir / "install" / "game.exe").write_text("game", encoding="utf-8")
    path = _write_config(tmp_path, {old: {"backend": "wine"}})

    cfg = Config.load(path)

    assert list(cfg.profiles) == [new]
    assert not old_dir.exists()
    assert (paths.profiles_dir() / new / "install" / "game.exe").is_file()
    assert cfg.notice and old in cfg.notice and new in cfg.notice


def test_legacy_mangohud_profile_key_no_longer_wraps_the_launch(tmp_path, monkeypatch) -> None:
    ctx = make_context(tmp_path, "stove-czn", {"mangohud": True})
    backend = CznBackend(ctx)
    monkeypatch.setattr(czn_backend.shutil, "which", lambda *a, **k: "/usr/bin/mangohud")

    env: dict[str, str] = {}
    assert backend._mangohud_wrap(["game"], env) == ["game"]
    assert "MANGOHUD" not in env

    monkeypatch.setenv("NADARE_MANGOHUD", "1")
    assert backend._mangohud_wrap(["game"], env) == ["/usr/bin/mangohud", "--dlsym", "game"]


def test_moved_profile_folder_keeps_stored_paths_valid(tmp_path):
    from nadare.core import paths

    old_dir = paths.profiles_dir() / "Bad/Name"
    (old_dir / "install").mkdir(parents=True)
    (old_dir / "install" / "game.exe").write_text("x")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"profiles": {"Bad/Name": {"backend": "wine", "install_dir": str(old_dir / "install")}}}
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)

    (new_key,) = cfg.profiles
    assert (paths.profiles_dir() / new_key / "install" / "game.exe").is_file()
    assert cfg.profiles[new_key]["install_dir"] == str(paths.profiles_dir() / new_key / "install")
