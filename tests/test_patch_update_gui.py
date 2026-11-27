# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI Update patch button and background index refresh (#1866 follow-up)."""

from __future__ import annotations

import dataclasses
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import indexfetch, patches, patchspecs
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow

AZUR = {"backend": "azurpromilia-jp-yostar"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    page = GameView()
    yield page
    page.close()
    page.deleteLater()


def test_update_button_visible_only_when_switch_on_and_outdated(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))

    # Off + not outdated -> hidden
    view.set_patch(False, outdated=False)
    assert view.patch_update_btn.isHidden()

    # Off + outdated -> still hidden (switch is off)
    view.set_patch(False, outdated=True)
    assert view.patch_update_btn.isHidden()

    # On + not outdated -> hidden
    view.set_patch(True, outdated=False)
    assert view.patch_update_btn.isHidden()

    # On + outdated -> visible, enabled, plain tooltip
    view.set_patch(True, outdated=True)
    assert not view.patch_update_btn.isHidden()
    assert view.patch_update_btn.text() == "Update patch"
    assert view.patch_update_btn.isEnabled()
    assert view.patch_update_btn.toolTip() == "A newer copy of the English patch is available."

    # Disabled while busy or running like the switch
    view.set_busy("update_patch")
    assert not view.patch_update_btn.isEnabled()
    assert view.step_label.text() == "Updating the patch"
    view.finish("update_patch", 0)
    assert view.patch_update_btn.isEnabled()

    view.set_status(Status(state="installed", running=True))
    assert not view.patch_update_btn.isEnabled()
    assert (
        view.patch_update_btn.toolTip()
        == "The game is running: close it to update the English patch."
    )

    # Hidden when not installed
    view.set_status(Status(state="not_installed"))
    assert view.patch_update_btn.isHidden()


def test_apply_patch_switch_does_not_read_index_cache(view, monkeypatch):
    monkeypatch.setattr(
        patchspecs, "specs_for", lambda *a, **k: pytest.fail("_apply_patch_switch read cache")
    )
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))
    view.set_patch(True, outdated=True)
    assert not view.patch_switch.isHidden()
    assert not view.patch_update_btn.isHidden()


def test_clicking_update_button_emits_patch_update_requested(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))
    view.set_patch(True, outdated=True)
    clicked = []
    view.patch_update_requested.connect(clicked.append)
    view.patch_update_btn.click()
    assert clicked == ["azur"]


def _write_record(install_dir, archive_sha256: str) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "AzurPromiliaJP.exe").write_bytes(b"stub")
    record = patches.PatchRecord(
        id="kibotl-en",
        version="v1.0-3219598",
        archive_sha256=archive_sha256,
        applied_at=time.time(),
        files=(),
    )
    patches._save_state(install_dir, {"kibotl-en": record})


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _write_cached_index(base: str, archive_sha256: str) -> None:
    builtin = patchspecs.KIBOTL_EN
    doc = {
        "schema": 1,
        "id": builtin.id,
        "kind": AZUR["backend"],
        "version": builtin.version,
        "url": builtin.url,
        "archive_sha256": archive_sha256,
        "archive_size": 123456,
        "files": [
            {
                "member": item.member,
                "dest": item.dest,
                "sha256": item.sha256,
                "size": item.size,
                "before_sha256": item.before_sha256,
                "before_size": item.before_size,
            }
            for item in builtin.files
        ],
    }
    path = indexfetch.cache_path(base, "patches/kibotl-en.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_window_sync_patch_detects_outdated_without_network_and_clicking_starts_update(
    qapp, tmp_path, monkeypatch
):
    from nadare.core import holds

    base = "https://index.invalid/"
    monkeypatch.setenv("NADARE_INDEX_BASE", base)
    monkeypatch.setattr(holds, "load", lambda *a, **k: [])
    monkeypatch.setattr(
        indexfetch, "fetch_json", lambda *a, **k: pytest.fail("_sync_patch must not touch network")
    )
    monkeypatch.setattr(
        indexfetch, "open_bytes", lambda *a, **k: pytest.fail("_sync_patch must not touch network")
    )
    # Real specs_for -> indexfetch.read_cached path: seed a valid cached index doc.
    new_sha = "a" * 64
    _write_cached_index(base, new_sha)
    install_dir = tmp_path / "game"
    _write_record(install_dir, patchspecs.KIBOTL_EN.archive_sha256)

    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {**AZUR, "install_dir": str(install_dir)}
    window = MainWindow(config)
    try:
        window._current_profile = "azur"
        window.game.set_profile("azur", config.profiles["azur"])
        window.game.set_status(Status(state="installed"))
        started = []
        monkeypatch.setattr(window, "_start", lambda *args: started.append(args))

        window._sync_patch("azur")
        assert window.game.patch_switch.isChecked()
        assert not window.game.patch_update_btn.isHidden()

        window.game.patch_update_btn.click()
        assert started == [("azur", "update_patch", "kibotl-en")]

        # When record hash matches current spec, Update button is hidden.
        _write_record(install_dir, new_sha)
        window._sync_patch("azur")
        assert window.game.patch_switch.isChecked()
        assert window.game.patch_update_btn.isHidden()
    finally:
        _close(window, qapp)


def test_conftest_stubs_patch_index_refresh_by_default(qapp, tmp_path, monkeypatch):
    assert hasattr(MainWindow, "_real_start_patch_index_refresh")
    install_dir = tmp_path / "game"
    _write_record(install_dir, patchspecs.KIBOTL_EN.archive_sha256)
    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {**AZUR, "install_dir": str(install_dir)}

    queued = []

    class FakePool:
        def start(self, task):
            queued.append(task)

    monkeypatch.setattr(QThreadPool, "globalInstance", staticmethod(lambda: FakePool()))
    window = MainWindow(config)
    try:
        monkeypatch.setattr(window, "_start", lambda *a, **k: None)
        queued.clear()
        window.open_game("azur")
        assert queued == []
    finally:
        _close(window, qapp)


def test_open_game_starts_one_background_refresh_and_resyncs_current_profile(
    qapp, tmp_path, monkeypatch
):
    if hasattr(MainWindow, "_real_start_patch_index_refresh"):
        monkeypatch.setattr(
            MainWindow, "_start_patch_index_refresh", MainWindow._real_start_patch_index_refresh
        )
    install_dir = tmp_path / "game"
    _write_record(install_dir, patchspecs.KIBOTL_EN.archive_sha256)

    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {**AZUR, "install_dir": str(install_dir)}
    config.profiles["other"] = {"backend": "arknights-yostar", "install_dir": str(tmp_path / "ak")}

    queued_tasks = []

    class FakePool:
        def start(self, task):
            queued_tasks.append(task)

    monkeypatch.setattr(QThreadPool, "globalInstance", staticmethod(lambda: FakePool()))

    refreshed_kinds = []
    updated_spec = patches.PatchSpec(
        id=patchspecs.KIBOTL_EN.id,
        title=patchspecs.KIBOTL_EN.title,
        version="v1.1-3219598",
        url=patchspecs.KIBOTL_EN.url,
        archive_sha256="f" * 64,
        archive_size=12345,
        files=patchspecs.KIBOTL_EN.files,
    )
    current = {"spec": patchspecs.KIBOTL_EN}

    monkeypatch.setattr(
        patchspecs,
        "specs_for",
        lambda kind: (current["spec"],) if kind == "azurpromilia-jp-yostar" else (),
    )

    def fake_refresh(kind: str):
        refreshed_kinds.append(kind)
        current["spec"] = updated_spec
        return (updated_spec,)

    monkeypatch.setattr(patchspecs, "refresh", fake_refresh)

    window = MainWindow(config)
    try:
        monkeypatch.setattr(window, "_start", lambda *a, **k: None)
        window._snapshots["azur"] = {"status": Status(state="installed")}
        queued_tasks.clear()

        # Opening a game with no patches starts no refresh.
        window.open_game("other")
        assert queued_tasks == []

        # Opening a game with a patch starts ONE refresh.
        window.open_game("azur")
        assert len(queued_tasks) == 1
        assert window.game.patch_update_btn.isHidden()

        # Opening again while the first refresh is still in flight does NOT start a second one.
        window.open_game("azur")
        assert len(queued_tasks) == 1

        # Running the queued task refreshes and re-syncs the current page.
        queued_tasks[0].run()
        qapp.processEvents()
        assert refreshed_kinds == ["azurpromilia-jp-yostar"]
        assert not window.game.patch_update_btn.isHidden()
        assert window._patch_refreshing == set()
        assert window._patch_refresh_tasks == {}
    finally:
        _close(window, qapp)


def test_background_refresh_does_not_resync_when_profile_changed(qapp, tmp_path, monkeypatch):
    if hasattr(MainWindow, "_real_start_patch_index_refresh"):
        monkeypatch.setattr(
            MainWindow, "_start_patch_index_refresh", MainWindow._real_start_patch_index_refresh
        )
    # The cached index pins a newer upload than the built-in one; the record is the built-in.
    index_pin = dataclasses.replace(patchspecs.KIBOTL_EN, archive_sha256="a" * 64)
    monkeypatch.setattr(
        patchspecs, "specs_for", lambda kind: (index_pin,) if kind == AZUR["backend"] else ()
    )
    install_dir = tmp_path / "game"
    _write_record(install_dir, patchspecs.KIBOTL_EN.archive_sha256)
    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {**AZUR, "install_dir": str(install_dir)}
    config.profiles["other"] = {"backend": "arknights-yostar", "install_dir": str(tmp_path / "ak")}

    queued_tasks = []

    class FakePool:
        def start(self, task):
            queued_tasks.append(task)

    monkeypatch.setattr(QThreadPool, "globalInstance", staticmethod(lambda: FakePool()))
    monkeypatch.setattr(patchspecs, "refresh", lambda kind: patchspecs.specs_for(kind))

    window = MainWindow(config)
    try:
        monkeypatch.setattr(window, "_start", lambda *a, **k: None)
        queued_tasks.clear()
        synced = []
        orig_sync = window._sync_patch
        monkeypatch.setattr(window, "_sync_patch", lambda p: synced.append(p) or orig_sync(p))

        window.open_game("azur")
        assert len(queued_tasks) == 1
        synced.clear()

        # User leaves azur for another profile before refresh finishes.
        window.open_game("other")
        synced.clear()

        queued_tasks[0].run()
        qapp.processEvents()
        assert synced == []
        assert window._patch_refreshing == set()
        assert window._patch_refresh_tasks == {}
    finally:
        _close(window, qapp)


def test_background_refresh_resyncs_second_profile_of_same_kind(qapp, tmp_path, monkeypatch):
    if hasattr(MainWindow, "_real_start_patch_index_refresh"):
        monkeypatch.setattr(
            MainWindow, "_start_patch_index_refresh", MainWindow._real_start_patch_index_refresh
        )
    dir1 = tmp_path / "game1"
    dir2 = tmp_path / "game2"
    _write_record(dir1, patchspecs.KIBOTL_EN.archive_sha256)
    _write_record(dir2, patchspecs.KIBOTL_EN.archive_sha256)

    config = Config.load(tmp_path / "config.json")
    config.profiles["azur1"] = {**AZUR, "install_dir": str(dir1)}
    config.profiles["azur2"] = {**AZUR, "install_dir": str(dir2)}

    queued_tasks = []

    class FakePool:
        def start(self, task):
            queued_tasks.append(task)

    monkeypatch.setattr(QThreadPool, "globalInstance", staticmethod(lambda: FakePool()))

    updated_spec = dataclasses.replace(patchspecs.KIBOTL_EN, archive_sha256="e" * 64)
    current = {"spec": patchspecs.KIBOTL_EN}
    monkeypatch.setattr(
        patchspecs, "specs_for", lambda kind: (current["spec"],) if kind == AZUR["backend"] else ()
    )

    def fake_refresh(kind: str):
        current["spec"] = updated_spec
        return (updated_spec,)

    monkeypatch.setattr(patchspecs, "refresh", fake_refresh)

    window = MainWindow(config)
    try:
        monkeypatch.setattr(window, "_start", lambda *a, **k: None)
        window._snapshots["azur1"] = {"status": Status(state="installed")}
        window._snapshots["azur2"] = {"status": Status(state="installed")}
        queued_tasks.clear()

        window.open_game("azur1")
        assert len(queued_tasks) == 1
        window.open_game("azur2")
        assert len(queued_tasks) == 1
        assert window.game.patch_update_btn.isHidden()

        queued_tasks[0].run()
        qapp.processEvents()
        assert not window.game.patch_update_btn.isHidden()
        assert window._patch_refreshing == set()
        assert window._patch_refresh_tasks == {}
    finally:
        _close(window, qapp)
