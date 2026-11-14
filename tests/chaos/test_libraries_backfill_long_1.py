# SPDX-License-Identifier: GPL-3.0-or-later
"""Start-up backfill must survive a recorded game folder path that cannot be stat'ed."""

from __future__ import annotations

from nadare.core import libraries
from nadare.core.config import Config


def test_backfill_does_not_crash_on_a_too_long_game_folder(tmp_path):
    games = tmp_path / "Games"
    nadare = games / libraries.NADARE_DIR
    nadare.mkdir(parents=True)
    game_dir = nadare / ("a" * 300)  # parent exists, component is over NAME_MAX

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "game",
        backend="direct-bd2",
        title="Game",
        install_dir=str(game_dir / "install"),
        owned_dirs=[str(game_dir)],
        owned_trees=["install_dir"],
        owned_paths={"install_dir": str(game_dir / "install")},
    )

    try:
        changed = libraries.backfill(cfg)
    except OSError as exc:
        print("game folder component length:", len(game_dir.name))
        print("backfill raised:", type(exc).__name__, exc)
        raise AssertionError(
            "an unstattable recorded game folder made backfill() raise OSError"
        ) from exc

    print("backfill returned:", changed)
    print("profile still present:", "game" in cfg.profiles)
    assert "game" in cfg.profiles
