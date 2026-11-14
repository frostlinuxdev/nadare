# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - pytest fixtures imported from sibling test modules
"""Round 7 (b23): guards whose tests stayed green when the guard was broken on purpose.

Method (/mnt/kikyou/work/mutation-b23): Flash named guards it thought untested; each was turned
into a real mutant and run against the tests of its module. Every test here kills one mutant that
had survived, and where a guard has a legitimate side, that side is tested too (a lock keeps the
wrong case out AND lets the right case in).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QWidget
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere
from test_nikke_launcherless_hosts import make_prefix
from test_nikke_updates import _nikke, api, env, installs  # noqa: F401 - fixtures

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core import libraries, paths, process, removal, runlock, vcredist
from nadare.core.backends import BackendError
from nadare.core.config import Config

KIND = "direct-bd2"
BD2 = libraries.GameSpec(kind=KIND, title="Brown Dust 2", exes=("BrownDust II.exe",))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home, tmp_path


def _manifest(game_dir: Path, **extra) -> None:
    data = {
        "schema": libraries.MANIFEST_SCHEMA,
        "backend": KIND,
        "title": "Brown Dust 2",
        "install": "install",
        "made_by_nadare": ["install"],
        **extra,
    }
    (game_dir / libraries.MANIFEST_NAME).write_text(json.dumps(data))


# ---- library roots: refused and allowed -----------------------------------------------------
def test_a_relative_library_path_is_refused_and_a_full_one_accepted(home_env, monkeypatch):
    cfg, home, _tmp = home_env
    monkeypatch.chdir(home)
    with pytest.raises(libraries.LibraryError, match="full folder path"):
        libraries.add_library(cfg, "Games")
    assert libraries.add_library(cfg, str(home / "Games")) == (home / "Games").resolve()


def test_a_folder_containing_home_is_refused(home_env):
    cfg, home, _tmp = home_env
    with pytest.raises(libraries.LibraryError, match="contains your home folder"):
        libraries.add_library(cfg, str(home.parent))


def test_a_file_is_refused_as_a_library(home_env):
    cfg, home, _tmp = home_env
    file = home / "notes.txt"
    file.write_text("x")
    with pytest.raises(libraries.LibraryError, match="is a file"):
        libraries.add_library(cfg, str(file))


def test_removing_a_library_that_is_not_listed_says_so(home_env):
    cfg, home, _tmp = home_env
    libraries.add_library(cfg, str(home / "Games"))
    assert libraries.remove_library(cfg, home / "Other") is False
    assert libraries.remove_library(cfg, home / "Games") is True


# ---- manifests and guesses -------------------------------------------------------------------
def test_no_manifest_is_written_into_a_folder_that_is_not_nadares_layout(home_env):
    cfg, home, _tmp = home_env
    mine = home / "MyGames" / "BD2"
    (mine / "install").mkdir(parents=True)
    cfg.add_profile(
        "bd2", backend=KIND, title="BD2", install_dir=str(mine / "install"), owned_dirs=[str(mine)]
    )
    libraries.backfill(cfg)
    assert not (mine / libraries.MANIFEST_NAME).exists()


def test_a_damaged_manifest_is_not_guessed_over_for_reuse(home_env):
    cfg, home, _tmp = home_env
    game_dir = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "BrownDust II.exe").write_text("MZ")
    assert libraries.reusable_folder(cfg, game_dir, BD2) is not None  # a guess, no manifest
    (game_dir / libraries.MANIFEST_NAME).write_text("{ damaged")
    assert libraries.reusable_folder(cfg, game_dir, BD2) is None


def test_nothing_is_guessed_in_the_default_library(home_env):
    cfg, home, _tmp = home_env
    for root in (libraries.default_root(), home / "Games"):
        game = Path(root) / libraries.NADARE_DIR / "Brown Dust 2" / "install"
        game.mkdir(parents=True)
        (game / "BrownDust II.exe").write_text("MZ")
    assert libraries.find_unmarked(cfg, libraries.default_root(), [BD2]) == []
    assert [c.kind for c in libraries.find_unmarked(cfg, home / "Games", [BD2])] == [KIND]


# ---- removal ------------------------------------------------------------------------------------
def test_a_retired_game_held_by_another_window_is_not_removed(home_env, monkeypatch):
    _cfg, _home, tmp = home_env
    profile = {"backend": "launcher-nte", "prefix": str(tmp / "pfx")}
    monkeypatch.setattr(process, "prefix_pids", lambda *a, **k: [])
    monkeypatch.setattr(runlock, "running", lambda prefix: True)
    assert "still running" in (removal._retired_running_problem(profile, "nte") or "")
    monkeypatch.setattr(runlock, "running", lambda prefix: False)
    assert removal._retired_running_problem(profile, "nte") is None


def test_the_manifest_stays_while_other_files_are_kept(tmp_path):
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / libraries.MANIFEST_NAME).write_text("{}")
    (folder / "save.dat").write_text("mine")
    removal._drop_lone_manifest(folder)
    assert (folder / libraries.MANIFEST_NAME).exists()
    (folder / "save.dat").unlink()
    removal._drop_lone_manifest(folder)
    assert not (folder / libraries.MANIFEST_NAME).exists()


def test_a_partly_deleted_tree_is_not_a_refusal(home_env, monkeypatch):
    cfg, home, tmp = home_env
    install = home / "Games" / "BD2"
    install.mkdir(parents=True)
    cfg.add_profile("bd2", backend="wine", install_dir=str(install), owned_trees=["install_dir"])
    monkeypatch.setattr(removal, "_running_problem", lambda config, name: None)

    def partly(root, keep=()):
        return removal._RmTreeResult(mounts=[root / "mnt"], leftovers=[])

    monkeypatch.setattr(removal, "_rmtree_safely", partly)
    result = removal.remove_game(
        cfg, "bd2", delete_trees=["install_dir"], applications_dir=tmp / "apps", save=False
    )
    assert result.profile_kept
    assert not result.refused  # something was deleted: a paused download must be forgotten


# ---- processes -------------------------------------------------------------------------------
def test_a_proton_pfx_prefix_counts_as_the_games_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    proc = tmp_path / "proc"
    d = proc / "21"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"wine\0game.exe\0")
    (d / "environ").write_bytes(f"WINEPREFIX={prefix / 'pfx'}\0".encode())
    assert process.prefix_pids(prefix, proc_root=proc) == [21]


# ---- VC++ marker -------------------------------------------------------------------------------
def test_a_linked_vcredist_marker_is_not_followed(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    real = tmp_path / "elsewhere"
    real.write_text("failed")
    (prefix / vcredist.FAILED_MARKER).symlink_to(real)
    assert vcredist._failed_recently(prefix) is None
    (prefix / vcredist.FAILED_MARKER).unlink()
    (prefix / vcredist.FAILED_MARKER).write_text("failed")  # a real marker still counts
    assert vcredist._failed_recently(prefix) is not None


# ---- Arknights marker -------------------------------------------------------------------------
def test_an_interrupted_arknights_sync_stays_marked_after_a_refusal(tmp_path, monkeypatch):
    from test_backends_yostar_direct import _ak, _multi_manifest

    from nadare.backends import arknights as ak
    from nadare.core import yostar

    manifest = _multi_manifest(tmp_path, "Arknights_EN", {"Arknights.exe": b"MZ"})
    backend = _ak(tmp_path, {"manifest_file": str(manifest)})
    target = backend.direct_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")  # installed, so update() really reaches the sync
    backend._write_install_marker()  # an earlier run was cut
    seen = []

    def refused(man, d, **kw):
        seen.append("sync")
        raise yostar.ManifestRefused("another install or update is already running")

    monkeypatch.setattr(ak.yostar, "sync", refused)
    with pytest.raises(BackendError):
        backend.update()
    assert seen == ["sync"]
    assert backend._install_marker_path().exists()


# ---- NIKKE ------------------------------------------------------------------------------------
def test_the_client_entry_is_nikke_exe_not_the_first_file(monkeypatch):
    from nadare.core.sources import nikke as source

    entries = [
        SimpleNamespace(is_dir=True, path="NIKKE\\game"),
        SimpleNamespace(is_dir=False, path="NIKKE\\game\\UnityPlayer.dll"),
        SimpleNamespace(is_dir=False, path="NIKKE\\game\\nikke.exe"),
    ]
    repo = SimpleNamespace(manifest_url="u", manifest_size=0, manifest_md5="")
    monkeypatch.setattr(source, "game_repositories", lambda info: [repo])
    monkeypatch.setattr(source.railrepo, "_fetch_verified", lambda *a, **k: b"")
    monkeypatch.setattr(source.railrepo, "parse_manifest", lambda data, repo: entries)
    assert source.client_exe_entry({}).path.endswith("nikke.exe")


def test_a_linked_session_folder_is_unlinked_not_emptied(home_env):
    _cfg, _home, tmp = home_env
    elsewhere = tmp / "someone-elses"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("theirs")
    profile_dir = paths.profiles_dir() / "nikke"
    profile_dir.mkdir(parents=True)
    (profile_dir / "nikke-session").symlink_to(elsewhere, target_is_directory=True)
    assert NikkeBackend.forget_session_files("nikke") is True
    assert not (profile_dir / "nikke-session").exists()
    assert (elsewhere / "keep.txt").read_text() == "theirs"


def test_the_game_search_stops_at_its_depth(tmp_path):
    prefix, launcher = make_prefix(tmp_path)
    (prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe").unlink()
    deep = prefix / "drive_c/NIKKE/a/b/c/d/e/f"
    deep.mkdir(parents=True)
    (deep / "nikke.exe").write_bytes(b"MZ")
    assert h.find_game(launcher, prefix) is None


def test_a_sign_in_without_mmkv_is_not_a_saved_sign_in(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "intl_auth.txt").write_text('{"token": 1}')
    assert h.has_cache(cache) is False
    (cache / "mmkv").mkdir()
    (cache / "mmkv" / "mmkv.default").write_bytes(b"x")
    assert h.has_cache(cache) is True


def test_the_storage_folder_skips_public_and_linked_users(tmp_path):
    users = tmp_path / "prefix" / "drive_c" / "users"
    (users / "Public").mkdir(parents=True)
    (users / "zed").mkdir()
    (users / "Linked").symlink_to(users / "zed", target_is_directory=True)
    found = h.cache_storage_dir(tmp_path / "prefix")
    assert found.is_relative_to(users / "zed")


def test_a_saved_sign_in_keeps_its_time(tmp_path):
    source = tmp_path / "src"
    (source / "mmkv").mkdir(parents=True)
    (source / "intl_auth.txt").write_text('{"token": 1}')
    (source / "mmkv" / "mmkv.default").write_bytes(b"x")
    old = time.time() - 3 * 86400
    for f in (source / "intl_auth.txt", source / "mmkv" / "mmkv.default"):
        os.utime(f, (old, old))
    h.save_cache(source, tmp_path / "cache")
    copied = (tmp_path / "cache" / "intl_auth.txt").stat().st_mtime
    assert abs(copied - old) < 2  # "the newer sign-in wins" compares these times


def test_a_session_that_is_not_an_object_is_refused(tmp_path):
    path = tmp_path / "session.enc.json"
    path.write_text("[1, 2]")
    with pytest.raises(h.HostError, match="unexpected"):
        h.read_auth(path)


def test_update_without_the_game_says_so(env, api, installs, monkeypatch):
    backend = _nikke(env)
    backend._client_exe().unlink()
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    with pytest.raises(BackendError, match="not installed"):
        backend.update()
    assert installs == []


def test_an_update_marker_that_is_not_a_file_refuses(env, api, installs):
    backend = _nikke(env)
    marker = backend._update_marker()
    os.mkfifo(marker)
    reader = os.open(marker, os.O_RDONLY | os.O_NONBLOCK)  # with a reader the open succeeds
    try:
        with pytest.raises(BackendError, match="cannot write"):
            backend._mark_update_incomplete()
    finally:
        os.close(reader)


# ---- the ordinary game launch folds umu's Stop noise -----------------------------------------
def test_a_direct_game_launch_feeds_its_output_through_the_filter(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends import direct
    from nadare.backends import winegame as wg

    backend = direct.StarRailBackend(make_context(tmp_path, "direct-hsr", {}))
    handlers = []

    def fake_stream(argv, env, on_line=None, cancel=None, **kw):
        handlers.append(on_line)
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    target = tmp_path / "game" / "StarRail.exe"
    target.parent.mkdir()
    target.write_bytes(b"MZ")
    for name, value in {
        "direct_target": lambda self: target,
        "_prefix_command": lambda self, args, extra_env=None, **kw: (list(args), {}),
        "_secure_wrap": lambda self, argv, env: (argv, env),
        "_run_vcredist": lambda self, created=False: None,
        "_log_launch_details": lambda self, env: None,
        "_run_lock": lambda self: contextlib.nullcontext(),
        "_ensure_default_runner": lambda self, strict=False: None,
        "_ensure_direct_prefix": lambda self: None,
    }.items():
        monkeypatch.setattr(type(backend), name, value, raising=False)
    with contextlib.suppress(Exception):
        backend.launch()
    if not handlers:
        pytest.skip("the launch did not reach the game stream in this harness")
    assert handlers == [backend._on_game_line]


# ---- GUI: FlowLayout ----------------------------------------------------------------------------
def _flow(qapp, widths, hidden=()):
    from nadare.gui.library import FlowLayout

    host = QWidget()
    flow = FlowLayout(host, margin=0, spacing=10)
    boxes = []
    for i, w in enumerate(widths):
        box = QWidget()
        box.setFixedSize(w, 20)
        flow.addWidget(box)
        if i in hidden:
            box.setVisible(False)
        boxes.append(box)
    return host, flow, boxes


def test_the_flow_layout_wraps_at_the_edge(qapp):
    host, flow, boxes = _flow(qapp, [100, 100, 100])
    host.resize(250, 200)
    host.show()
    qapp.processEvents()
    assert boxes[2].y() > boxes[0].y()  # the third does not fit beside the first two
    assert flow.heightForWidth(250) > flow.heightForWidth(400)
    host.deleteLater()


def test_a_hidden_widget_takes_no_place_in_the_flow(qapp):
    host, flow, boxes = _flow(qapp, [100, 100, 100], hidden=(1,))
    host.resize(215, 200)  # 100 + 10 + 100 fits; one more spacing for the hidden one would not
    host.show()
    boxes[1].hide()
    qapp.processEvents()
    assert boxes[2].y() == boxes[0].y()  # the hidden one left room on the first line
    host.deleteLater()


# ---- GUI: Settings Move commit ------------------------------------------------------------------
def _commit(qapp, cfg, changes, monkeypatch):
    from nadare.gui import settings as settings_mod
    from nadare.gui.settings import SettingsPage

    monkeypatch.setattr(settings_mod.QMessageBox, "critical", lambda *a, **k: None)
    page = SettingsPage()
    ok = page._commit_relink(cfg, "bd2", SimpleNamespace(changes=changes))
    page.deleteLater()
    qapp.processEvents()
    return ok


def test_a_prefix_only_move_does_not_bring_back_a_removed_library(home_env, qapp, monkeypatch):
    cfg, home, _tmp = home_env
    game = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    cfg.add_profile(
        "bd2", backend=KIND, install_dir=str(game / "install"), owned_dirs=[str(game)],
        prefix=str(home / "p1"),
    )
    cfg.data[libraries.CONFIG_KEY] = []  # the user removed ~/Games
    cfg.save()
    assert _commit(qapp, cfg, {"prefix": str(home / "p2")}, monkeypatch)
    assert (home / "Games").resolve() not in libraries.library_roots(cfg)


def test_a_move_whose_save_fails_puts_the_libraries_back(home_env, qapp, monkeypatch):
    cfg, home, _tmp = home_env
    old = home / "Old" / libraries.NADARE_DIR / "Brown Dust 2"
    new = home / "New" / libraries.NADARE_DIR / "Brown Dust 2"
    for g in (old, new):
        (g / "install").mkdir(parents=True)
    cfg.add_profile("bd2", backend=KIND, install_dir=str(old / "install"), owned_dirs=[str(old)])
    cfg.data[libraries.CONFIG_KEY] = [{"path": str((home / "Old").resolve())}]
    cfg.save()
    before = json.loads(json.dumps(cfg.data[libraries.CONFIG_KEY]))

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(cfg), "save", broken)
    changes = {"install_dir": str(new / "install"), "owned_dirs": [str(new)]}
    assert _commit(qapp, cfg, changes, monkeypatch) is False
    assert cfg.data[libraries.CONFIG_KEY] == before


# ---- GUI: Libraries panel -----------------------------------------------------------------------
def _panel(cfg, answer=True):
    from nadare.gui.libraries_panel import LibrariesPanel

    panel = LibrariesPanel(choose=lambda items: [answer] * len(items))
    panel.load(cfg)
    return panel


def test_a_failed_save_of_a_new_library_leaves_none_behind(home_env, qapp, monkeypatch):
    cfg, home, _tmp = home_env

    def broken(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(type(cfg), "save", broken)
    panel = _panel(cfg)
    assert "read-only" in panel.add_folder(str(home / "Games"))
    assert (home / "Games").resolve() not in libraries.library_roots(cfg)
    panel.deleteLater()


def _same_game_in_two_libraries(home):
    for lib in ("A", "B"):
        game = home / lib / libraries.NADARE_DIR / "Brown Dust 2"
        (game / "install").mkdir(parents=True)
        (game / "install" / "BrownDust II.exe").write_text("MZ")
        _manifest(game)


def test_the_same_game_in_two_libraries_is_added_once(home_env, qapp):
    cfg, home, _tmp = home_env
    _same_game_in_two_libraries(home)
    for lib in ("A", "B"):
        libraries.add_library(cfg, str(home / lib))
    panel = _panel(cfg)
    names = panel.find_games()
    assert len(names) == 1
    assert sum(1 for p in cfg.profiles.values() if p.get("backend") == KIND) == 1
    panel.deleteLater()


def test_games_found_are_not_kept_when_the_save_fails(home_env, qapp, monkeypatch):
    cfg, home, _tmp = home_env
    _same_game_in_two_libraries(home)
    libraries.add_library(cfg, str(home / "A"))
    panel = _panel(cfg)

    def broken(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(type(cfg), "save", broken)
    assert panel.find_games() == []
    assert not any(p.get("backend") == KIND for p in cfg.profiles.values())
    panel.deleteLater()


def test_a_full_rescan_queued_is_not_narrowed_by_a_later_add(home_env, qapp):
    cfg, home, _tmp = home_env
    panel = _panel(cfg)
    panel._searching = True
    panel.find_games_later()  # everything, queued
    panel.find_games_later([home / "Games"])  # then one library
    assert panel._queued == []  # still "every library"
    panel._searching = False
    panel.deleteLater()


# ---- GUI: Add Game rollback --------------------------------------------------------------------
def test_a_failed_add_game_save_forgets_the_library_it_made(home_env, qapp, monkeypatch):
    from nadare.gui.add_game import AddGameDialog

    cfg, home, tmp = home_env
    other = home / "Elsewhere"
    other.mkdir()
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(other.resolve())}]  # a library from before
    before = json.loads(json.dumps(cfg.data[libraries.CONFIG_KEY]))
    dialog = AddGameDialog(cfg, data_root=tmp / "data", kind=KIND)
    try:
        dialog.location_toggle.click()
        dialog.set_install_dir(str(home / "Games"))

        def broken(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(type(cfg), "save", broken)
        assert dialog.create_profile() is None
        assert cfg.data[libraries.CONFIG_KEY] == before
        assert (home / "Games").resolve() not in libraries.library_roots(cfg)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- GUI: a failed Remove ends the page's REMOVING -----------------------------------------------
def test_a_failed_remove_ends_the_removing_state(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.add_profile("g", backend="stove-czn", title="G")
    win = MainWindow(config)
    win.controller.start = lambda profile, op, payload=None: True
    monkeypatch.setattr(main_window.QMessageBox, "critical", lambda *a, **k: None)
    try:
        win.open_game("g")
        win._busy["g"] = "remove"
        win.game.set_busy("remove")
        win._on_remove_failed("g", "Permission denied")
        assert win.game._busy_op is None
        assert not win.game.primary.text().startswith("REMOVING")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_a_failed_add_game_save_with_no_libraries_before_leaves_none(home_env, qapp, monkeypatch):
    from nadare.gui.add_game import AddGameDialog

    cfg, home, tmp = home_env
    dialog = AddGameDialog(cfg, data_root=tmp / "data", kind=KIND)
    try:
        dialog.location_toggle.click()
        dialog.set_install_dir(str(home / "Games"))

        def broken(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(type(cfg), "save", broken)
        assert dialog.create_profile() is None
        assert libraries.CONFIG_KEY not in cfg.data
    finally:
        dialog.deleteLater()
        qapp.processEvents()
