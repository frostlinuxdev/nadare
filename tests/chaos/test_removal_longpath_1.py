# SPDX-License-Identifier: GPL-3.0-or-later
"""A configured folder path that is too long must be refused, not crash Remove."""

from __future__ import annotations

from nadare.core import removal
from nadare.core.config import Config


def test_a_too_long_install_path_keeps_the_profile_with_a_warning(tmp_path):
    games = tmp_path / "Games"
    games.mkdir()
    too_long = games / ("a" * 300)  # this cannot exist: one component over NAME_MAX

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "gone",
        backend="wine",
        title="Gone",
        install_dir=str(too_long),
        owned_trees=["install_dir"],
        owned_paths={"install_dir": str(too_long)},
    )
    cfg.save()

    try:
        result = removal.remove_game(
            cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp_path / "apps"
        )
    except OSError as exc:
        print("configured install path length:", len(too_long.name))
        print("remove_game raised:", type(exc).__name__, exc)
        raise AssertionError(
            "an unusable configured path made remove_game() raise OSError"
        ) from exc

    print("warnings:", result.warnings)
    print("profile kept:", result.profile_kept, "gone present:", "gone" in cfg.profiles)

    assert "gone" in cfg.profiles
    assert result.profile_kept
    assert result.trees_removed == []
    assert any("install" in warning.lower() for warning in result.warnings), result.warnings
