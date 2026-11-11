# SPDX-License-Identifier: GPL-3.0-or-later
"""Libraries in the GUI: the Settings card, the install dialog's picker, remembered folders."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QFileDialog
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import libraries, paths
from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui.add_game import AddGameDialog
from nadare.gui.libraries_panel import LibrariesPanel
from nadare.gui.quick_install import QuickInstallDialog

KIND = "direct-bd2"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home, tmp_path


def _library_game(root: Path, folder: str = "Brown Dust 2") -> Path:
    game_dir = root / libraries.NADARE_DIR / folder
    (game_dir / "install").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": KIND,
                "title": "Brown Dust 2",
                "install": "install",
                "made_by_nadare": ["install"],
            }
        )
    )
    return game_dir


# ---- the Settings card ----------------------------------------------------------------------


def _settle(qapp) -> None:
    """Let Find games' background scan finish and deliver its answer."""
    from PyQt6.QtCore import QThreadPool

    for _ in range(3):
        assert QThreadPool.globalInstance().waitForDone(10_000)
        qapp.processEvents()


def _panel(cfg, qapp, answer=True):
    asked: list[str] = []

    def choose(items):
        asked.append("\n".join(label for label, _checked in items))
        return [True] * len(items) if answer else None

    panel = LibrariesPanel(choose=choose)
    panel.load(cfg)
    qapp.processEvents()
    return panel, asked


def test_the_panel_lists_the_default_library_without_a_remove_button(env, qapp):
    cfg, _home, _tmp = env
    panel, _ = _panel(cfg, qapp)
    rows = panel.rows()
    assert [r.path for r in rows] == [paths.profiles_dir().resolve()]
    assert rows[0].is_default
    assert rows[0].remove_button is None
    assert rows[0].label.text().endswith(" free")  # a number, not "free space unknown"


def test_adding_a_library_saves_it_at_once(env, qapp, tmp_path):
    cfg, home, tmp = env
    lib = home / "Games"
    lib.mkdir()
    panel, _ = _panel(cfg, qapp)
    assert panel.add_folder(str(lib)) == ""
    _settle(qapp)
    assert lib.resolve() in [r.path for r in panel.rows()]
    again = Config.load(tmp / "config.json")
    assert lib.resolve() in libraries.library_roots(again)


def test_a_refused_library_shows_why(env, qapp):
    cfg, home, _tmp = env
    panel, _ = _panel(cfg, qapp)
    message = panel.add_folder(str(home))
    _settle(qapp)
    assert "home folder" in message
    assert panel.message.text() == message
    assert len(panel.rows()) == 1


def test_removing_a_library_keeps_its_files_and_saves(env, qapp):
    cfg, home, tmp = env
    lib = home / "Games"
    game_dir = _library_game(lib)
    panel, asked = _panel(cfg, qapp, answer=False)  # "found games" question: not now
    panel.add_folder(str(lib))
    _settle(qapp)
    row = next(r for r in panel.rows() if r.path == lib.resolve())
    row.remove_button.click()
    assert lib.resolve() not in [r.path for r in panel.rows()]
    assert (game_dir / "install").is_dir()
    assert lib.resolve() not in libraries.library_roots(Config.load(tmp / "config.json"))


def test_adding_a_library_with_games_offers_them_and_adds_them(env, qapp):
    cfg, home, tmp = env
    lib = home / "Games"
    _library_game(lib)
    panel, asked = _panel(cfg, qapp, answer=True)
    added: list[list[str]] = []
    panel.games_added.connect(added.append)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert asked and "Brown Dust 2" in asked[0]
    assert added == [["bd2"]]
    saved = Config.load(tmp / "config.json")
    assert saved.profiles["bd2"]["backend"] == KIND


def test_saying_no_adds_no_games(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib)
    panel, asked = _panel(cfg, qapp, answer=False)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert asked
    assert "bd2" not in cfg.profiles


def test_find_games_looks_in_every_library(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    lib.mkdir()
    panel, asked = _panel(cfg, qapp, answer=True)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert asked == []  # nothing there yet: no question
    _library_game(lib)
    panel.find_button.click()
    _settle(qapp)
    assert asked
    assert "bd2" in cfg.profiles


def test_find_games_with_nothing_new_says_so(env, qapp):
    cfg, _home, _tmp = env
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    _settle(qapp)
    assert asked == []
    assert "No new games" in panel.message.text()


# ---- Settings embeds it ---------------------------------------------------------------------


def test_settings_shows_the_libraries_card_in_the_all_games_tab(env, qapp):
    from nadare.gui.settings import SettingsPage

    cfg, _home, _tmp = env
    cfg.add_profile("sample", backend="wine")
    page = SettingsPage()
    try:
        page.load(cfg, "sample")
        panel = page.libraries_panel
        tab = next(
            page.tabs.tabText(i)
            for i in range(page.tabs.count())
            if page.tabs.widget(i).isAncestorOf(panel)
        )
        assert tab == "Nadare (all games)"
        assert panel.rows()[0].is_default
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- the install dialog's picker ------------------------------------------------------------


def test_the_install_dialog_offers_the_libraries(qapp, monkeypatch):
    seen: list[str | None] = []

    def describe(chosen):
        seen.append(chosen)
        return (chosen or "/default/game", "10 GB free", "")

    dialog = QuickInstallDialog(
        "Game",
        notice="",
        describe=describe,
        libraries=[("Default", ""), ("/games", "/games"), ("/drive", "/drive")],
    )
    try:
        combo = dialog.library_combo
        assert [combo.itemText(i) for i in range(combo.count())] == ["Default", "/games", "/drive"]
        combo.setCurrentIndex(2)
        assert seen[-1] == "/drive"
        assert dialog.chosen_folder == "/drive"
        combo.setCurrentIndex(0)
        assert seen[-1] is None
        assert dialog.chosen_folder == ""
    finally:
        dialog.deleteLater()


def test_a_folder_picked_with_change_location_joins_the_picker(qapp, monkeypatch):
    def describe(chosen):
        return (chosen or "/default/game", "10 GB free", "")

    dialog = QuickInstallDialog("Game", notice="", describe=describe, libraries=[("Default", "")])
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "/new/place")
    try:
        dialog.change_button.click()
        combo = dialog.library_combo
        assert combo.currentData() == "/new/place"
        assert combo.count() == 2
    finally:
        dialog.deleteLater()


def test_the_install_dialog_without_libraries_has_no_picker(qapp):
    dialog = QuickInstallDialog("Game", notice="", describe=lambda c: ("/x", "", ""))
    try:
        assert dialog.library_combo is None
    finally:
        dialog.deleteLater()


# ---- Add Game remembers a picked folder ------------------------------------------------------


def _add_dialog(cfg, tmp, qapp, kind="arknights-yostar"):
    d = AddGameDialog(cfg, data_root=tmp / "data", kind=kind)
    d.show()
    qapp.processEvents()
    return d


def test_a_picked_folder_becomes_a_library(env, qapp):
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(home / "Games"))
        assert d.create_profile()
        assert (home / "Games").resolve() in libraries.library_roots(
            Config.load(tmp / "config.json")
        )
    finally:
        d.close()


def test_the_default_place_adds_no_library(env, qapp):
    cfg, _home, tmp = env
    d = _add_dialog(cfg, tmp, qapp)
    try:
        assert d.create_profile()
        assert libraries.library_roots(cfg) == [paths.profiles_dir().resolve()]
    finally:
        d.close()


def test_use_default_location_undoes_a_picked_folder(env, qapp):
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp)
    try:
        default_folder = d.folder.text()
        default_prefix = d.prefix.text()
        d.set_install_dir(str(home / "Games"))
        assert d.folder.text() != default_folder
        d.use_default_location()
        assert d.folder.text() == default_folder
        assert d.prefix.text() == default_prefix
        name = d.create_profile()
        assert "owned_dirs" not in d.config.profile(name)
    finally:
        d.close()


def test_folder_names_for_libraries_match_add_game(env):
    """The panel and Add Game agree on the <library>/Nadare/<Game> layout."""
    assert libraries.NADARE_DIR == "Nadare"
    assert add_game._folder_name("Brown Dust 2") == "Brown Dust 2"


def test_library_paths_under_home_are_shown_short(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    lib.mkdir()
    panel, _ = _panel(cfg, qapp)
    panel.add_folder(str(lib))
    _settle(qapp)
    row = next(r for r in panel.rows() if r.path == lib.resolve())
    assert row.label.text().startswith("~/Games")
    assert row.label.toolTip() == str(lib.resolve())
    assert libraries.display_path(Path("/mnt/ssd/Games")) == "/mnt/ssd/Games"


# ---- startup backfill (step 3) ---------------------------------------------------------------


def test_startup_backfill_saves_only_when_something_changed(env, monkeypatch):
    from types import SimpleNamespace

    from nadare.gui.main_window import MainWindow

    cfg, home, tmp = env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    cfg.add_profile(
        "bd2",
        backend=KIND,
        title="BD2",
        install_dir=str(game_dir / "install"),
        owned_dirs=[str(game_dir)],
    )
    cfg.save()
    saves = []
    real_save = cfg.save
    monkeypatch.setattr(cfg, "save", lambda: saves.append(1) or real_save())
    MainWindow._backfill_libraries(SimpleNamespace(config=cfg))
    assert saves == [1]
    assert (home / "Games").resolve() in libraries.library_roots(Config.load(tmp / "config.json"))
    MainWindow._backfill_libraries(SimpleNamespace(config=cfg))
    assert saves == [1]


def test_startup_backfill_never_raises(env, monkeypatch):
    from types import SimpleNamespace

    from nadare.gui.main_window import MainWindow

    cfg, _home, _tmp = env

    def boom(_config):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(libraries, "backfill", boom)
    MainWindow._backfill_libraries(SimpleNamespace(config=cfg))  # must not raise


# ---- step 4: reuse a game folder already in the library ---------------------------------------


def _bd2_dialog(cfg, tmp, qapp):
    return _add_dialog(cfg, tmp, qapp, kind="direct-bd2")


def _bare_bd2(lib: Path, manifest: bool = False) -> Path:
    game_dir = lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "BrownDust II.exe").write_text("x")
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    if manifest:
        (game_dir / libraries.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "backend": KIND,
                    "title": "Brown Dust 2",
                    "install": "install",
                    "prefix": "prefix",
                    "made_by_nadare": ["install", "prefix"],
                }
            )
        )
    return game_dir


def test_an_install_reuses_the_games_folder_already_in_the_library(env, qapp):
    cfg, home, tmp = env
    lib = home / "Games"
    game_dir = _bare_bd2(lib)
    d = _bd2_dialog(cfg, tmp, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(lib))
        _settle(qapp)  # the reuse look-up runs off the GUI thread (#1207 b150)
        assert "files already there" in d.install_summary.full_text()
        name = d.create_profile()
        prof = d.config.profile(name)
        assert prof["install_dir"] == str(game_dir.resolve() / "install")
        assert prof["prefix"] == str(game_dir.resolve() / "prefix")
        assert "owned_trees" not in prof  # no manifest: the files may be the user's
        assert not (lib / libraries.NADARE_DIR / "Brown Dust 2 2").exists()
    finally:
        d.close()


def test_a_reused_folder_with_its_manifest_keeps_its_ownership(env, qapp):
    cfg, home, tmp = env
    lib = home / "Games"
    _bare_bd2(lib, manifest=True)
    d = _bd2_dialog(cfg, tmp, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(lib))
        prof = d.config.profile(d.create_profile())
        assert set(prof["owned_trees"]) == {"install_dir", "prefix"}
    finally:
        d.close()


def test_a_folder_another_game_uses_is_not_reused(env, qapp):
    cfg, home, tmp = env
    lib = home / "Games"
    game_dir = _bare_bd2(lib)
    cfg.add_profile("other", backend="wine", install_dir=str(game_dir / "install"))
    d = _bd2_dialog(cfg, tmp, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(lib))
        prof = d.config.profile(d.create_profile())
        assert "Brown Dust 2 2" in prof["install_dir"]
    finally:
        d.close()


# ---- step 4: Find games with checkboxes -------------------------------------------------------


def test_find_games_offers_guesses_unticked_and_manifests_ticked(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib, folder="Marked")  # manifest, kind direct-bd2
    seen: list[list[tuple[str, bool]]] = []

    def choose(items):
        seen.append(items)
        return [checked for _label, checked in items]  # accept the defaults

    panel = LibrariesPanel(choose=choose)
    panel.load(cfg)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert len(seen) == 1 and seen[0][0][1] is True
    assert "bd2" in cfg.profiles


def test_a_guessed_game_is_added_only_when_ticked(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    _bare_bd2(lib)
    answers = iter([[False], [True]])
    seen = []

    def choose(items):
        seen.append(items)
        return next(answers)

    panel = LibrariesPanel(choose=choose)
    panel.load(cfg)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert seen and seen[0][0][1] is False  # a guess starts unticked
    assert "guessed" in seen[0][0][0]
    assert "bd2" not in cfg.profiles
    panel.find_button.click()
    _settle(qapp)
    assert "bd2" in cfg.profiles
    assert "owned_trees" not in cfg.profiles["bd2"]


def test_cancelling_the_choice_adds_nothing(env, qapp):
    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib)
    panel = LibrariesPanel(choose=lambda items: None)
    panel.load(cfg)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert "bd2" not in cfg.profiles


def test_the_all_games_tab_fits_the_smallest_window(env, qapp):
    """Seen in the b23-rc1 click-through: the Libraries card's long subtitle did not wrap, so the
    tab wanted 1206 px in a 638 px view and pushed Remove off-screen."""
    from nadare.gui.settings import SettingsPage

    cfg, _home, _tmp = env
    cfg.add_profile("sample", backend="wine")
    page = SettingsPage()
    try:
        page.load(cfg, "sample")
        page.resize(720, 700)  # the settings area at Nadare's minimum window width (880)
        page.show()
        qapp.processEvents()
        index = next(
            i for i in range(page.tabs.count()) if page.tabs.tabText(i) == "Nadare (all games)"
        )
        scroll = page.tabs.widget(index)
        assert scroll.widget().minimumSizeHint().width() <= scroll.viewport().width()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_reused_manifest_does_not_claim_a_prefix_the_user_picked(env, qapp):
    """Round-1 review 2026-11-11: the old manifest's "prefix made by Nadare" must not
    transfer to a prefix the user chose instead, or Remove would offer to delete it."""
    cfg, home, tmp = env
    lib = home / "Games"
    _bare_bd2(lib, manifest=True)
    own = home / "MyPrefixes" / "bd2"
    (own / "drive_c").mkdir(parents=True)
    (own / "drive_c" / "keep.txt").write_text("mine")
    d = _bd2_dialog(cfg, tmp, qapp)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(lib))
        d.set_prefix(str(own))
        prof = d.config.profile(d.create_profile())
        assert Path(prof["prefix"]).resolve() == own.resolve()
        assert "prefix" not in prof.get("owned_trees", [])
        assert "prefix" not in prof.get("owned_paths", {})
        assert "install_dir" in prof.get("owned_trees", [])
    finally:
        d.close()


def test_a_missing_library_does_not_show_another_drives_free_space(tmp_path):
    from nadare.gui.libraries_panel import _free_text

    assert "not found" in _free_text(tmp_path / "unplugged" / "Games")
    assert "free" in _free_text(tmp_path)


def test_a_library_whose_folder_is_missing_is_not_offered_for_installs(env, qapp):
    """b23-rc4 click-through: an unplugged drive's library was offered; installing there makes
    the folders on whatever disk is underneath (the system disk for an empty mount point)."""
    from nadare.gui.main_window import MainWindow

    cfg, home, _tmp = env
    there = home / "Games"
    there.mkdir()
    libraries.add_library(cfg, there)
    libraries.add_library(cfg, home / "Unplugged" / "Games")
    cfg.save()
    window = MainWindow(cfg)
    try:
        folders = [folder for _label, folder in window._library_choices()]
        assert "" in folders  # the default library is always there (made on demand)
        assert str(there.resolve()) in folders
        assert not any("Unplugged" in folder for folder in folders)
    finally:
        window.close()
        window.deleteLater()


# ---- Find games runs off the GUI thread ---------------------------------------------------------


def test_find_games_scans_on_a_worker_thread(env, qapp, monkeypatch):
    import threading

    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib)
    threads: list[threading.Thread] = []
    real_scan = libraries.scan

    def scan(config, root):
        threads.append(threading.current_thread())
        return real_scan(config, root)

    monkeypatch.setattr(libraries, "scan", scan)
    panel, asked = _panel(cfg, qapp)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert threads and all(t is not threading.main_thread() for t in threads)
    assert asked and "bd2" in cfg.profiles


def test_find_games_is_disabled_while_it_looks_and_runs_a_second_request_after(
    env, qapp, monkeypatch
):
    import threading

    cfg, home, _tmp = env
    lib = home / "Games"
    lib.mkdir()
    gate = threading.Event()
    calls: list[Path] = []
    real_scan = libraries.scan

    def slow_scan(config, root):
        calls.append(root)
        gate.wait(10)
        return real_scan(config, root)

    panel, asked = _panel(cfg, qapp)
    panel.add_folder(str(lib))
    _settle(qapp)
    monkeypatch.setattr(libraries, "scan", slow_scan)
    calls.clear()
    panel.find_button.click()
    assert panel.searching
    assert not panel.find_button.isEnabled()
    assert "Looking" in panel.message.text()
    from PyQt6.QtCore import QThreadPool

    real_start = QThreadPool.start
    started: list[object] = []
    def counting_start(pool, task, *args):
        started.append(task)
        return real_start(pool, task, *args)

    monkeypatch.setattr(QThreadPool, "start", counting_start)
    panel.find_games_later()  # a second request while the first runs waits for it
    assert started == []  # round 7 (mutation): never a second scan beside the first
    gate.set()
    _settle(qapp)
    assert not panel.searching and panel.find_button.isEnabled()
    assert len(calls) == 2 * len(libraries.library_roots(cfg))  # then once more, in turn
    assert "No new games" in panel.message.text()


def test_a_game_added_while_the_scan_ran_is_not_offered(env, qapp, monkeypatch):
    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib)
    real_scan = libraries.scan

    def scan_then_add(config, root):
        result = real_scan(config, root)
        # The user adds the same game another way while the worker is busy.
        if "other" not in cfg.profiles:
            cfg.add_profile("other", backend=KIND)
        return result

    monkeypatch.setattr(libraries, "scan", scan_then_add)
    panel, asked = _panel(cfg, qapp)
    panel.add_folder(str(lib))
    _settle(qapp)
    assert asked == []
    assert "bd2" not in cfg.profiles


def test_a_failed_scan_is_reported_and_find_games_works_again(env, qapp, monkeypatch):
    cfg, _home, _tmp = env

    def broken(config, root):
        raise OSError("Input/output error")

    monkeypatch.setattr(libraries, "scan", broken)
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    _settle(qapp)
    assert asked == []
    assert "Input/output error" in panel.message.text()
    assert panel.find_button.isEnabled() and not panel.searching


def test_closing_the_panel_while_it_looks_does_not_crash(env, qapp, monkeypatch):
    import threading

    from PyQt6 import sip

    cfg, home, _tmp = env
    lib = home / "Games"
    _library_game(lib)
    gate = threading.Event()
    real_scan = libraries.scan

    def slow_scan(config, root):
        gate.wait(10)
        return real_scan(config, root)

    monkeypatch.setattr(libraries, "scan", slow_scan)
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    sip.delete(panel)
    gate.set()
    _settle(qapp)
    assert asked == []


def test_a_prefix_only_game_reuses_its_folder_already_in_the_library(env, qapp):
    """Round 4: CZN (prefix only) made "<Game> 2" and downloaded everything again."""
    cfg, home, tmp = env
    lib = home / "Games"
    game_dir = lib / libraries.NADARE_DIR / "Chaos Zero Nightmare"
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "stove-czn",
                "title": "Chaos Zero Nightmare",
                "prefix": "prefix",
                "made_by_nadare": ["prefix"],
            }
        )
    )
    d = _add_dialog(cfg, tmp, qapp, kind="stove-czn")
    try:
        d.location_toggle.click()
        d.set_prefix(str(lib))
        name = d.create_profile()
        assert name
        prof = d.config.profile(name)
        assert prof["prefix"] == str(game_dir.resolve() / "prefix")
        assert prof["owned_trees"] == ["prefix"]
        assert not (lib / libraries.NADARE_DIR / "Chaos Zero Nightmare 2").exists()
    finally:
        d.close()


# ---- a picked "Nadare" folder is the library's game folder, not a new library -----------------
# Maintainer, 2026-11-11: picking /mnt/x/Games/Nadare (where the games are) planned
# /mnt/x/Games/Nadare/Nadare/<Game>. The folder Nadare makes inside a library means that library.


def test_picking_the_nadare_folder_uses_its_library(env, qapp):
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp, kind=KIND)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(home / "Games" / "Nadare"))
        assert d.create_profile()
        profile = next(iter(Config.load(tmp / "config.json").profiles.values()))
        install = Path(profile["install_dir"])
        assert "Nadare/Nadare" not in install.as_posix()
        assert install.parent == (home / "Games" / "Nadare").resolve() or install.parent.parent == (
            home / "Games" / "Nadare"
        ).resolve()
        roots = libraries.library_roots(Config.load(tmp / "config.json"))
        assert (home / "Games").resolve() in roots
        assert (home / "Games" / "Nadare").resolve() not in roots
    finally:
        d.close()


def test_picking_the_nadare_folder_with_a_trailing_slash_too(env, qapp):
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp, kind=KIND)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(home / "Games" / "Nadare") + "/")
        assert d.create_profile()
        profile = next(iter(Config.load(tmp / "config.json").profiles.values()))
        assert "Nadare/Nadare" not in Path(profile["install_dir"]).as_posix()
    finally:
        d.close()


def test_picking_the_nadare_folder_finds_a_game_already_there(env, qapp):
    """The reuse of a game's folder (#1167) works through the Nadare folder too."""
    cfg, home, tmp = env
    game_dir = _library_game(home / "Games")
    d = _add_dialog(cfg, tmp, qapp, kind=KIND)
    try:
        d.location_toggle.click()
        d.set_install_dir(str(home / "Games" / "Nadare"))
        d._plan_paths(d._selected_game(), d.folder.text().strip(), d.prefix.text().strip(), False)
        assert d.reused is not None and d.reused.game_dir == game_dir.resolve()
    finally:
        d.close()


def test_a_nadare_folder_in_home_stays_a_library_of_its_own(env):
    _cfg, home, _tmp = env
    assert libraries.library_of(home / "Nadare") == home / "Nadare"
    assert libraries.library_of(home / "Games" / "Nadare") == home / "Games"
    assert libraries.library_of(home / "Games") == home / "Games"



def test_picking_the_nadare_folder_for_nikke_uses_its_library(env, qapp):
    """The maintainer's case: NIKKE from the Store into /mnt/x/Games/Nadare."""
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp, kind="launcher-nikke")
    try:
        d.location_toggle.click()
        d.set_install_dir(str(home / "Games" / "Nadare"))  # what the Store dialog does
        assert d.create_profile()
        profile = next(iter(Config.load(tmp / "config.json").profiles.values()))
        for key in ("install_dir", "prefix"):
            assert "Nadare/Nadare" not in Path(profile[key]).as_posix(), key
        assert Path(profile["prefix"]).parent.parent == (home / "Games" / "Nadare").resolve()
    finally:
        d.close()


def test_picking_the_nadare_folder_for_a_prefix_only_game_uses_its_library(env, qapp):
    cfg, home, tmp = env
    d = _add_dialog(cfg, tmp, qapp, kind="stove-czn")
    try:
        d.location_toggle.click()
        d.set_prefix(str(home / "Games" / "Nadare"))  # the whole install is the prefix
        assert d.create_profile()
        profile = next(iter(Config.load(tmp / "config.json").profiles.values()))
        prefix = Path(profile["prefix"])
        assert "Nadare/Nadare" not in prefix.as_posix()
        assert prefix.parent.parent == (home / "Games" / "Nadare").resolve()
    finally:
        d.close()


def test_picking_a_librarys_nadare_folder_selects_that_library(qapp, monkeypatch, tmp_path):
    """rc7 click-through: the picker listed <lib>/Nadare as a new entry next to <lib>."""
    lib = tmp_path / "lib"
    (lib / "Nadare").mkdir(parents=True)
    seen: list[str | None] = []

    def describe(chosen):
        seen.append(chosen)
        return (chosen or "/default/game", "10 GB free", "")

    dialog = QuickInstallDialog(
        "Game", notice="", describe=describe, libraries=[("Default", ""), (str(lib), str(lib))]
    )
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(lib / "Nadare") + "/"
    )
    try:
        dialog.change_button.click()
        combo = dialog.library_combo
        assert combo.count() == 2
        assert combo.currentData() == str(lib)
        assert seen[-1] == str(lib)
        assert dialog.chosen_folder == str(lib)
    finally:
        dialog.deleteLater()
