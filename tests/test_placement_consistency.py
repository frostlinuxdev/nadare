"""Placement rules shared by every kind: the library layout, absolute paths, validation and CZN."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui.add_game import AddGameDialog, detect_import


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def dialog(tmp_path, qapp, *, kind="wine"):
    instance = AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data", kind=kind
    )
    instance.show()
    qapp.processEvents()
    return instance


def close(d, qapp):
    d.deleteLater()
    qapp.processEvents()


def game_dir(library: Path, d: AddGameDialog) -> Path:
    return library / "Nadare" / add_game._folder_name(d._selected_game().title)


# ---- 1. CZN follows the same library rule as every other kind ----------------------
def test_czn_picked_library_gets_nadare_game_prefix(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        library = tmp_path / "Games"
        d.location_toggle.click()
        d.set_prefix(str(library))
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        nested = game_dir(library, d) / "prefix"
        assert profile["prefix"] == str(nested)
        assert profile.get("install_dir") == ""
        assert profile["owned_dirs"] == [str(nested.parent)]
        assert "prefix" in profile["owned_trees"]
        assert str(library) not in profile.get("owned_trees", [])
    finally:
        close(d, qapp)


def test_czn_library_rule_uses_the_same_free_folder_naming(tmp_path, qapp):
    library = tmp_path / "Games"
    first = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        first.location_toggle.click()
        first.set_prefix(str(library))
        assert first.create_profile()
    finally:
        close(first, qapp)
    second = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        second.set_profile_name("another")
        second.location_toggle.click()
        second.set_prefix(str(library))
        name = second.create_profile()
        assert name
        assert second.config.profile(name)["prefix"] == str(
            library / "Nadare" / "Chaos Zero Nightmare 2" / "prefix"
        )
    finally:
        close(second, qapp)


def test_czn_folder_that_already_holds_a_prefix_is_kept_and_not_owned(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        prefix = tmp_path / "MyPrefix"
        (prefix / "drive_c").mkdir(parents=True)
        d.location_toggle.click()
        d.set_prefix(str(prefix))
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        assert profile["prefix"] == str(prefix)
        assert "owned_trees" not in profile
        assert "owned_dirs" not in profile
    finally:
        close(d, qapp)


def test_czn_untouched_default_is_unchanged(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        assert profile["prefix"] == str(tmp_path / "data" / "chaos-zero-nightmare" / "prefix")
        assert "owned_dirs" not in profile
    finally:
        close(d, qapp)


# ---- 2. Stored paths are absolute (without resolving symlinks) ----------------------
def test_a_relative_library_is_stored_absolute(tmp_path, qapp, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir("Games")
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        final = game_dir(tmp_path / "Games", d)
        assert profile["install_dir"] == str(final / "install")
        assert profile["prefix"] == str(final / "prefix")
        assert profile["owned_dirs"] == [str(final)]
        stored = json.loads((tmp_path / "config.json").read_text())
        assert stored["profiles"][name]["install_dir"] == str(final / "install")
    finally:
        close(d, qapp)


def test_a_tilde_library_is_stored_expanded(tmp_path, qapp, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir("~/Games")
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        final = game_dir(home / "Games", d)
        assert profile["install_dir"] == str(final / "install")
        assert profile["prefix"] == str(final / "prefix")
        assert profile["owned_dirs"] == [str(final)]
    finally:
        close(d, qapp)


def test_an_absolute_symlink_spelling_is_kept_in_the_stored_path(tmp_path, qapp):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(link))
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        assert profile["install_dir"].startswith(str(link))
        assert str(real) not in profile["install_dir"]
    finally:
        close(d, qapp)


# ---- 3. A folder that cannot work is refused before anything is written ------------
def _readonly(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o555)


def _writable_again(path: Path) -> None:
    path.chmod(0o755)


def test_a_read_only_library_is_refused(tmp_path, qapp):
    library = tmp_path / "ReadOnly"
    _readonly(library)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(library))
        assert d.create_profile() is None
        assert "writable" in d.error_label.text().lower()
        assert not d.config.profiles
    finally:
        close(d, qapp)
        _writable_again(library)


def test_a_missing_folder_under_a_read_only_parent_is_refused(tmp_path, qapp):
    library = tmp_path / "ReadOnly"
    _readonly(library)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(library / "not" / "created"))
        assert d.create_profile() is None
        assert "writable" in d.error_label.text().lower()
        assert not d.config.profiles
    finally:
        close(d, qapp)
        _writable_again(library)


def test_a_read_only_prefix_is_refused(tmp_path, qapp):
    prefix = tmp_path / "ReadOnlyPrefix"
    _readonly(prefix)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(tmp_path / "Games"))
        d.prefix_toggle.click()
        d.set_prefix(str(prefix))
        assert d.create_profile() is None
        assert "writable" in d.error_label.text().lower()
        assert not d.config.profiles
    finally:
        close(d, qapp)
        _writable_again(prefix)


def test_a_missing_folder_under_a_writable_parent_is_still_allowed(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        library = tmp_path / "NotCreatedYet" / "Games"
        d.location_toggle.click()
        d.set_install_dir(str(library))
        name = d.create_profile()
        assert name
        assert not library.exists()
    finally:
        close(d, qapp)


# ---- 4. CZN import only accepts a folder in which a prefix was detected ------------
def test_detect_import_refuses_an_unrelated_czn_folder(tmp_path):
    empty = tmp_path / "TotallyEmpty"
    empty.mkdir()
    assert detect_import("stove-czn", empty, None)["notes"] == []
    assert detect_import("stove-czn", None, empty)["notes"] == []


def test_czn_import_of_an_empty_folder_is_refused(tmp_path, qapp):
    empty = tmp_path / "TotallyEmpty"
    empty.mkdir()
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        d.set_mode(True)
        d.set_prefix(str(empty))
        d.detect()
        assert not d.create_button.isEnabled()
        assert d.create_profile() is None
        assert "prefix" in d.error_label.text().lower()
        assert "drive_c" in d.error_label.text()
        assert not d.config.profiles
    finally:
        close(d, qapp)


def test_czn_import_accepts_a_folder_inside_its_prefix(tmp_path, qapp):
    prefix = tmp_path / "CZNPrefix"
    game = prefix / "drive_c" / "ProgramData" / "Smilegate" / "Games" / "ChaosZeroNightmare"
    game.mkdir(parents=True)
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        d.set_mode(True)
        d.set_install_dir(str(game))
        d.detect()
        assert d.create_button.isEnabled()
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        assert profile["prefix"] == str(prefix)
        assert not profile.get("owned_trees")
    finally:
        close(d, qapp)


# ---- 5. Import keeps working from a folder Nadare must not write to --------------
def test_importing_from_a_read_only_library_is_allowed(tmp_path, qapp):
    game = tmp_path / "library" / "Arknights"
    game.mkdir(parents=True)
    (game / "Arknights.exe").write_text("exe")
    _readonly(game)
    d = dialog(tmp_path, qapp, kind="arknights-yostar")
    try:
        d.set_mode(True)
        d.set_install_dir(str(game))
        d.detect()
        assert d.create_button.isEnabled()
        name = d.create_profile()
        assert name
        assert d.config.profile(name)["install_dir"] == str(game)
    finally:
        close(d, qapp)
        _writable_again(game)


# ---- 6. CZN shows the prefix it will really store --------------------------------
def test_czn_fresh_install_shows_the_nested_prefix_it_stores(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        d.location_toggle.click()
        d.set_prefix(str(tmp_path / "Games"))
        qapp.processEvents()
        assert d.prefix_only_summary.isVisible()
        assert not d.prefix_only_summary.isHidden()
        shown = d.prefix_only_summary.full_text()
        name = d.create_profile()
        assert name
        assert shown == f"Wine prefix: {d.config.profile(name)['prefix']}"
    finally:
        close(d, qapp)


# ---- 7. A path through a symlink and '..' means what the kernel resolves -----------
def test_a_typed_path_through_a_symlink_and_dotdot_lands_where_it_points(tmp_path, qapp):
    real = tmp_path / "real" / "deep"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    typed = str(link / ".." / "Games")
    assert add_game._absolute(typed) == os.path.realpath(typed)
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(typed)
        name = d.create_profile()
        assert name
        final = game_dir(tmp_path / "real" / "Games", d)
        assert d.config.profile(name)["install_dir"] == str(final / "install")
    finally:
        close(d, qapp)


# ---- 8. A fresh CZN install never nests a prefix inside another prefix -------------
def test_a_fresh_czn_install_inside_another_prefix_is_refused(tmp_path, qapp):
    picked = tmp_path / "other-game-prefix" / "drive_c" / "Games" / "Other"
    picked.mkdir(parents=True)
    d = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        d.location_toggle.click()
        d.set_prefix(str(picked))
        assert d.create_profile() is None
        assert d.error_label.text() == (
            "That folder is inside another Wine prefix. Pick a folder outside it."
        )
        assert not d.config.profiles
    finally:
        close(d, qapp)


# ---- 9. '~' is expanded before an import is detected --------------------------------
def test_a_tilde_import_is_detected_and_stored_absolute(tmp_path, qapp, monkeypatch):
    home = tmp_path / "home"
    game = home / "Games" / "Arknights"
    game.mkdir(parents=True)
    (game / "Arknights.exe").write_text("exe")
    czn_prefix = home / "CZNPrefix"
    (czn_prefix / "drive_c").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert detect_import("arknights-yostar", "~/Games/Arknights", None)["install_dir"] == str(game)
    assert detect_import("stove-czn", None, "~/CZNPrefix")["prefix"] == str(czn_prefix)
    d = dialog(tmp_path, qapp, kind="arknights-yostar")
    try:
        d.set_mode(True)
        d.set_install_dir("~/Games/Arknights")
        d.detect()
        assert d.create_button.isEnabled()
        name = d.create_profile()
        assert name
        assert d.config.profile(name)["install_dir"] == str(game)
    finally:
        close(d, qapp)


def test_an_ordinary_games_prefix_line_stays_next_to_the_install_line(tmp_path, qapp):
    """The CZN row must not move the other kinds' summary away from "Installs to:"."""
    d = AddGameDialog(Config.load(tmp_path / "c.json"), data_root=tmp_path / "data", kind="wine")
    try:
        d.resize(640, 560)
        d.show()
        d.location_toggle.click()
        qapp.processEvents()
        assert not d.install_summary.isHidden() and not d.prefix_summary.isHidden()
        assert d.prefix_only_summary.isHidden()
        gap = d.prefix_summary.geometry().top() - d.install_summary.geometry().bottom()
        assert 0 <= gap <= 10, "nothing may sit between the two location lines"
    finally:
        d.close()
        d.deleteLater()
        qapp.processEvents()
