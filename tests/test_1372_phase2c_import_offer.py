# SPDX-License-Identifier: GPL-3.0-or-later
"""Existing imported profiles remain movable through Settings."""

from nadare.core import importmove
from nadare.core.config import Config


def _import_shape(tmp_path):
    install = tmp_path / "old" / "WW"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "g.pak").write_bytes(b"x" * 100)
    prefix = tmp_path / "old" / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    return install, prefix


# ---- core: when is an import movable --------------------------------------
def test_an_import_with_its_prefix_is_movable(tmp_path):
    install, prefix = _import_shape(tmp_path)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(install), prefix=str(prefix),
    )
    assert importmove.movable(config, "wuwa") is True


def test_a_game_nadare_made_is_not_offered(tmp_path):
    install, prefix = _import_shape(tmp_path)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(install), prefix=str(prefix), owned_dirs=[str(tmp_path / "old")],
    )
    assert importmove.movable(config, "wuwa") is False


def test_an_import_without_a_prefix_is_not_offered(tmp_path):
    install, _ = _import_shape(tmp_path)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(install), prefix=str(tmp_path / "missing"),
    )
    assert importmove.movable(config, "wuwa") is False
    assert importmove.movable(config, "nobody") is False


def test_an_import_whose_folder_is_gone_is_not_offered(tmp_path):
    _, prefix = _import_shape(tmp_path)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(tmp_path / "gone"), prefix=str(prefix),
    )
    assert importmove.movable(config, "wuwa") is False
