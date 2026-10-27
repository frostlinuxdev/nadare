from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui.add_game import AddGameDialog


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


def folder_row(d):
    return d._form.itemAt(0, d._form.ItemRole.FieldRole).widget()


def test_expanded_default_keeps_prefix_in_profile(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        default_folder = tmp_path / "data" / "wine-proton-game" / "install"
        default_prefix = tmp_path / "data" / "wine-proton-game" / "prefix"
        assert not folder_row(d).isHidden()
        assert d.prefix_row.isHidden()
        assert d.prefix_summary.full_text() == f"Wine prefix: {default_prefix}"
        assert d.folder.text() == str(default_folder)
        assert d.create_profile() == "wine-proton-game"
        p = d.config.profile("wine-proton-game")
        assert p["install_dir"] == str(default_folder)
        assert p["prefix"] == str(default_prefix)
    finally:
        d.close()


def test_folder_moves_default_prefix_and_two_games_are_distinct(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.set_install_dir("/x/y/Games/Brown Dust 2")
        assert d.prefix.text() == "/x/y/Games/Brown Dust 2-prefix"
        assert d.prefix_summary.full_text() == f"Wine prefix: {d.prefix.text()}"
        d.set_install_dir("/x/y/BD2/install")
        assert d.prefix.text() == "/x/y/BD2/prefix"
        d.set_install_dir("/x/y/Games/One")
        one = d.prefix.text()
        d.set_install_dir("/x/y/Games/Two")
        assert d.prefix.text() != one
    finally:
        d.close()


def test_advanced_prefix_stops_following_and_persists(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.prefix_toggle.click()
        assert not d.prefix_row.isHidden()
        d.prefix.setText("/custom/prefix")
        d.prefix.textEdited.emit(d.prefix.text())
        d.prefix_toggle.click()
        assert d.prefix_row.isHidden()
        d.set_install_dir("/other/game")
        assert d.prefix.text() == "/custom/prefix"
        assert d.create_profile() == "wine-proton-game"
        assert d.config.profile("wine-proton-game")["prefix"] == "/custom/prefix"
    finally:
        d.close()


def test_game_change_closes_advanced_toggle_and_resets_default(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        d.prefix_toggle.click()
        d.set_backend("arknights-yostar")
        assert d.prefix_toggle.text() == "Choose the Wine prefix separately…"
        assert d.prefix_row.isHidden()
        assert d.prefix.text() == str(tmp_path / "data" / "arknights" / "prefix")
    finally:
        d.close()


def test_import_and_prefix_only_layouts_remain_unchanged(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.set_mode(True)
        assert not folder_row(d).isHidden() and not d.prefix_row.isHidden()
        assert d.folder_label.text() == "Existing game folder"
        assert d.prefix.text() == ""
        assert not d.create_button.isEnabled()
    finally:
        d.close()
    czn = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        czn.location_toggle.click()
        assert czn.folder_label.text() == "Game folder"
        assert not czn.prefix_row.isHidden()  # prefix-only location is the game folder
        assert czn.prefix_summary.isHidden() and czn.prefix_toggle.isHidden()
    finally:
        czn.close()


def test_existing_profile_config_is_not_rewritten(tmp_path, qapp):
    path = tmp_path / "config.json"
    existing = {
        "schema": 1,
        "profiles": {
            "old": {
                "backend": "wine",
                "title": "Old",
                "install_dir": "/games/old",
                "prefix": "/prefix/old",
            }
        },
    }
    path.write_text(json.dumps(existing, indent=2) + "\n")
    before = path.read_bytes()
    d = dialog(tmp_path, qapp)
    try:
        d.close()
        qapp.processEvents()
        assert path.read_bytes() == before
    finally:
        d.deleteLater()


def test_catalog_handoff_uses_new_location_layout(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="arknights-yostar")
    try:
        d.location_toggle.click()
        assert not folder_row(d).isHidden()
        assert d.prefix_row.isHidden()
        assert not d.prefix_summary.isHidden()
    finally:
        d.close()


# ---- review additions: the form row, empty/odd folders and very long paths ----
def prefix_label(d):
    return d._form.labelForField(d.prefix_row)


def test_default_expanded_view_has_no_orphan_wine_prefix_label(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="direct-bd2")
    try:
        d.location_toggle.click()
        qapp.processEvents()
        assert prefix_label(d).isHidden() and d.prefix_row.isHidden()
        assert d.prefix_free.isHidden()
        d.prefix_toggle.click()
        assert not prefix_label(d).isHidden() and not d.prefix_row.isHidden()
        assert prefix_label(d).text() == "Wine prefix"
        d.prefix_toggle.click()
        assert prefix_label(d).isHidden() and d.prefix_row.isHidden()
    finally:
        d.close()


def test_import_and_czn_still_show_their_prefix_row_with_its_label(tmp_path, qapp):
    imported = dialog(tmp_path, qapp, kind="direct-bd2")
    try:
        imported.set_mode(True)
        assert not prefix_label(imported).isHidden()
        assert not imported.prefix_row.isHidden() and not imported.prefix_free.isHidden()
    finally:
        imported.close()
    czn = dialog(tmp_path, qapp, kind="stove-czn")
    try:
        czn.location_toggle.click()
        assert not prefix_label(czn).isHidden() and prefix_label(czn).text() == "Game folder"
        assert not czn.prefix_free.isHidden()
    finally:
        czn.close()


def test_a_cleared_or_rootless_game_folder_never_gives_a_relative_prefix(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="direct-bd2")
    try:
        d.location_toggle.click()
        default_prefix = str(d._root() / d._slug() / "prefix")
        for odd in ("", "   ", "/", "."):
            d.folder.setText(odd)
            assert os.path.isabs(d.prefix.text()), (odd, d.prefix.text())
            assert d.prefix.text() == default_prefix, (odd, d.prefix.text())
        d.folder.setText("")
        name = d.create_profile()
        assert name
        profile = d.config.profile(name)
        assert os.path.isabs(profile["prefix"]) and not profile.get("install_dir")
    finally:
        d.close()


def test_a_very_long_path_does_not_widen_the_dialog(tmp_path, qapp):
    from PyQt6.QtWidgets import QScrollArea

    d = dialog(tmp_path, qapp, kind="direct-bd2")
    try:
        d.location_toggle.click()
        qapp.processEvents()
        long_folder = "/mnt/" + "a-very-long-folder-name/" * 12 + "game"
        d.set_install_dir(long_folder)
        qapp.processEvents()
        scroll = d.findChildren(QScrollArea)[0]
        assert scroll.horizontalScrollBar().maximum() == 0
        full = d.prefix_summary.full_text()
        assert full == f"Wine prefix: {long_folder}-prefix"
        assert d.prefix_summary.toolTip() == full
        assert d.prefix_summary.text() != full  # shortened to fit, not wrapped or clipped
    finally:
        d.close()


# ---- picking an existing folder must not turn it into the game folder (#560) -------
def _create(d, folder):
    d.location_toggle.click()
    d.set_install_dir(str(folder))
    name = d.create_profile()
    assert name
    return d.config.profile(name)


def test_a_picked_new_folder_gets_nadare_game_install_and_prefix(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        folder = tmp_path / "Games"
        game_dir = folder / "Nadare" / add_game._folder_name(d._selected_game().title)
        profile = _create(d, folder)
        assert profile["install_dir"] == str(game_dir / "install")
        assert profile["prefix"] == str(game_dir / "prefix")
        assert set(profile["owned_trees"]) == {"install_dir", "prefix"}
        assert profile["owned_dirs"] == [str(game_dir)]
    finally:
        d.close()


def test_an_existing_nadare_folder_is_reused(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        folder = tmp_path / "Games"
        (folder / "Nadare" / "Some other game").mkdir(parents=True)
        game_dir = folder / "Nadare" / add_game._folder_name(d._selected_game().title)
        profile = _create(d, folder)
        assert profile["install_dir"] == str(game_dir / "install")
        assert (folder / "Nadare" / "Some other game").is_dir()
    finally:
        d.close()


def test_the_untouched_default_location_is_not_nested(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        d.location_toggle.click()
        profile_name = d.create_profile()
        profile = d.config.profile(profile_name)
        assert profile["install_dir"] == str(tmp_path / "data" / "wine-proton-game" / "install")
        assert "owned_dirs" not in profile
    finally:
        d.close()


def test_a_folder_with_other_things_in_it_gets_nadare_game_install_and_prefix(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        library = tmp_path / "Games"
        (library / "other game").mkdir(parents=True)
        (library / "other game" / "x.exe").write_text("x")
        game_dir = library / "Nadare" / add_game._folder_name(d._selected_game().title)
        profile = _create(d, library)
        assert profile["install_dir"] == str(game_dir / "install")
        assert profile["prefix"] == str(game_dir / "prefix")
        assert set(profile["owned_trees"]) == {"install_dir", "prefix"}
        assert profile["owned_dirs"] == [str(game_dir)]
        assert (library / "other game" / "x.exe").exists()
    finally:
        d.close()


def test_a_second_profile_of_the_same_game_gets_the_next_free_folder(tmp_path, qapp):
    library = tmp_path / "Games"
    (library / "other game").mkdir(parents=True)
    (library / "other game" / "x.exe").write_text("x")
    first = dialog(tmp_path, qapp)
    try:
        name = add_game._folder_name(first._selected_game().title)
        _create(first, library)
    finally:
        first.close()
    second = dialog(tmp_path, qapp)
    try:
        second.set_profile_name("another")
        profile = _create(second, library)
        assert profile["install_dir"] == str(library / "Nadare" / f"{name} 2" / "install")
    finally:
        second.close()


def test_the_chosen_paths_are_stored_not_recomputed(tmp_path, qapp):
    d = dialog(tmp_path, qapp)
    try:
        library = tmp_path / "Games"
        (library / "other").mkdir(parents=True)
        (library / "other" / "x").write_text("x")
        name = _create(d, library)["install_dir"]
        stored = json.loads((tmp_path / "config.json").read_text())
        (profile,) = stored["profiles"].values()
        assert profile["install_dir"] == name
    finally:
        d.close()


def test_a_folder_that_already_holds_this_game_is_kept_but_not_owned(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="arknights-yostar")
    try:
        game = d._selected_game()
        folder = tmp_path / "MyGame"
        folder.mkdir()
        (folder / game.exes[0]).write_text("x")
        profile = _create(d, folder)
        assert profile["install_dir"] == str(folder)
        assert "install_dir" not in profile.get("owned_trees", [])
    finally:
        d.close()


def test_an_imported_game_is_never_recorded_as_created_by_nadare(tmp_path, qapp):
    d = dialog(tmp_path, qapp, kind="arknights-yostar")
    try:
        game = d._selected_game()
        root = tmp_path / "pfx" / "drive_c" / game.install_subdir.replace("\\", "/")
        root.mkdir(parents=True)
        (root / game.exes[0]).write_text("x")
        d.set_mode(True)
        d.set_install_dir(str(root))
        name = d.create_profile()
        assert name
        assert not d.config.profile(name).get("owned_trees")
    finally:
        d.close()


def test_folder_names_drop_path_and_shell_hostile_characters():
    assert add_game._folder_name("Punishing: Gray Raven") == "Punishing Gray Raven"
    assert add_game._folder_name("Wine/Proton game") == "Wine Proton game"
    assert add_game._folder_name("///") == "game"
