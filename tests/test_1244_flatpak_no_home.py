# SPDX-License-Identifier: GPL-3.0-or-later
"""#1244: the Flatpak gets ~/Games and the shortcut folders, not the whole home folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import artwork, flatpakfs, paths

MANIFEST = Path(__file__).resolve().parents[1] / "packaging" / "flatpak" / (
    "io.github.frostlinuxdev.Nadare.yml"
)


def _finish_args() -> list[str]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [line.strip()[2:] for line in lines if line.strip().startswith("- --")]


def test_the_manifest_no_longer_grants_home():
    args = _finish_args()
    assert "--filesystem=home" not in args
    assert "--filesystem=host" not in args
    assert "--filesystem=~/Games:create" in args
    assert "--filesystem=xdg-data/applications" in args
    assert "--filesystem=xdg-desktop" in args
    for kept in ("/mnt", "/media", "/run/media"):
        assert f"--filesystem={kept}" in args


@pytest.fixture()
def flatpak(tmp_path, monkeypatch):
    info = tmp_path / "flatpak-info"
    grants = [a.split("=", 1)[1] for a in _finish_args() if a.startswith("--filesystem=")]
    info.write_text("[Context]\nfilesystems=" + ";".join(grants) + ";\n", encoding="utf-8")
    monkeypatch.setattr(flatpakfs, "FLATPAK_INFO", str(info))
    monkeypatch.setattr(flatpakfs.sandbox, "in_flatpak", lambda: True)
    return Path.home()


@pytest.mark.parametrize("rel", ["Games", "Games/Nadare/WuWa"])
def test_games_under_home_games_are_reachable(flatpak, rel):
    assert flatpakfs.unreachable_reason(flatpak / rel) is None


@pytest.mark.parametrize("rel", ["Spiele", ".ssh", "Documents/Games", ".mozilla"])
def test_other_home_folders_are_refused_with_the_override(flatpak, rel):
    reason = flatpakfs.unreachable_reason(flatpak / rel)
    assert reason and "flatpak override --user --filesystem=" in reason
    assert "your home folder" not in reason
    assert "~/Games" in reason


def test_drives_stay_reachable(flatpak):
    assert flatpakfs.unreachable_reason("/mnt/games") is None
    assert flatpakfs.unreachable_reason("/run/media/user/disk/Games") is None


# ---- custom images are copied into Nadare's own folder ---------------------------------------


def test_a_picked_image_is_copied_into_nadares_folder(tmp_path):
    picked = tmp_path / "Pictures" / "bg.png"
    picked.parent.mkdir(parents=True)
    picked.write_bytes(b"\x89PNGdata")
    kept = Path(artwork.keep_custom_image(str(picked), "wuwa"))
    assert paths.data_dir() in kept.parents
    assert kept.read_bytes() == b"\x89PNGdata"
    assert kept.suffix == ".png"


def test_an_image_already_in_nadares_folders_is_used_as_is(tmp_path):
    inside = paths.cache_dir() / "covers" / "x.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"jpg")
    assert artwork.keep_custom_image(str(inside), "wuwa") == str(inside)


def test_a_missing_or_empty_path_is_left_alone(tmp_path):
    assert artwork.keep_custom_image("", "wuwa") == ""
    missing = str(tmp_path / "gone.png")
    assert artwork.keep_custom_image(missing, "wuwa") == missing


def test_the_same_image_twice_is_one_copy(tmp_path):
    picked = tmp_path / "bg.png"
    picked.write_bytes(b"same")
    first = artwork.keep_custom_image(str(picked), "wuwa")
    assert artwork.keep_custom_image(str(picked), "wuwa") == first


def test_settings_stores_the_copy_of_a_picked_background(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from types import SimpleNamespace

    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui import settings as settings_mod

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        settings_mod, "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="not_installed", data={})
        ),
    )
    picked = tmp_path / "Pictures" / "bg.png"
    picked.parent.mkdir(parents=True)
    picked.write_bytes(b"\x89PNGbg")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("w", backend="wine", title="W")
    config.save()
    page = settings_mod.SettingsPage()
    try:
        page.load(config, "w")
        QThreadPool.globalInstance().waitForDone(5000)
        page.background.setText(str(picked))
        page.save()
        stored = Path(Config.load(config.path).profiles["w"]["custom_background"])
        assert paths.data_dir() in stored.parents
        assert stored.read_bytes() == b"\x89PNGbg"
    finally:
        page.deleteLater()
        app.processEvents()
