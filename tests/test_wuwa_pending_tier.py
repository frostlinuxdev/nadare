# SPDX-License-Identifier: GPL-3.0-or-later
"""#1335: a WuWa quality switch is a pending update, and only COMPLETED tiers count.

Before: the update check compared only the version, which is the same for every tier, so
after a switch to SD Nadare said "up to date" and SD was never downloaded. And a tier counted
as installed as soon as its folder held any file, so a half-downloaded tier could be launched
(crash) or make Nadare offer to delete the only complete tier.

Now a finished sync records its tier in the version file (``qualities``). Version files from
before this change have no record; then the tiers with files on disk count (as in #1333).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context

from nadare.backends.direct import WutheringWavesBackend
from nadare.core.gamefacts import primary_operation


def _backend(tmp_path: Path, quality: str = "hd") -> WutheringWavesBackend:
    values = {
        "install_dir": str(tmp_path / "wuwa"),
        "prefix": str(tmp_path / "prefix"),
        "wuwa_quality": quality,
    }
    return WutheringWavesBackend(make_context(tmp_path, "direct-wuwa", values))


def _install(backend: WutheringWavesBackend, version: str = "3.8.0", tiers=("HD",),
             record: list[str] | None = None) -> None:
    root = backend.install_dir()
    game = root / backend.GAME_EXE
    game.parent.mkdir(parents=True, exist_ok=True)
    game.write_bytes(b"MZ")
    (root / "Wuthering Waves.exe").write_bytes(b"MZ")
    for name in tiers:
        _tier(backend, name)
    backend._write_version_file(version, "wuwa")
    if record is not None:
        path = backend._version_path()
        data = json.loads(path.read_text())
        data["qualities"] = record
        path.write_text(json.dumps(data))
    backend._write_latest(version)


def _tier(backend: WutheringWavesBackend, name: str, size: int = 16) -> Path:
    folder = backend.install_dir() / "Client" / "Content" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"pakchunk1-{name}-WindowsNoEditor.pak").write_bytes(b"x" * size)
    return folder


def _rebind(backend: WutheringWavesBackend, quality: str) -> None:
    backend.ctx.profile["wuwa_quality"] = quality


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


# ------------------------------------------------------------------ status: pending tier
def test_a_switched_tier_shows_as_an_update(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record=["hd"])
    _rebind(backend, "sd")
    status = backend.status()
    assert status.data["update_available"] is True
    assert status.data["pending_quality"] == "sd"
    assert "SD" in str(status.data.get("latest_version"))
    assert primary_operation(status) == "update"


def test_the_installed_tier_is_up_to_date(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record=["hd"])
    status = backend.status()
    assert status.data["update_available"] is False
    assert "pending_quality" not in status.data


def test_a_b23_profile_poisoned_with_sd_shows_a_pending_update_and_deletes_nothing(tmp_path):
    """The real desktop: no ``qualities`` record (old version file), sd chosen, HD on disk."""
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD",), record=None)
    assert backend.status().data["update_available"] is True
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.other_quality_folders() == []


def test_no_tier_folders_at_all_is_no_pending_update(tmp_path):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=(), record=None)
    assert backend.status().data["update_available"] is False


# ------------------------------------------------------------------ partial tiers
def test_a_partial_tier_is_not_launched(tmp_path):
    """SD has files from a cancelled Update but was never recorded as complete."""
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record=["hd"])
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.status().data["update_available"] is True


def test_a_partial_chosen_tier_never_offers_to_delete_the_complete_one(tmp_path):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record=["hd"])
    assert backend.other_quality_folders() == []


# ------------------------------------------------------------------ a finished sync records
def test_a_finished_sync_records_its_tier(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record=["hd"])
    _rebind(backend, "sd")
    _tier(backend, "SD")
    backend._record_synced_version(SimpleNamespace(version="3.8.0", game_tag="wuwa"))
    data = json.loads(backend._version_path().read_text())
    assert sorted(data["qualities"]) == ["hd", "sd"]
    assert data["version"] == "3.8.0"
    assert backend.status().data["update_available"] is False
    assert backend.launch_extra_args()[-1] == "-krqlv=sd"
    assert [q for q, _p, _s in backend.other_quality_folders()] == ["hd"]


def test_a_first_record_keeps_the_tiers_already_on_disk(tmp_path):
    """Upgrading from an old version file: tiers with files stay counted after the next sync."""
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record=None)
    backend._before_sync()  # every real sync starts with it (round 2)
    backend._record_synced_version(SimpleNamespace(version="3.8.0", game_tag="wuwa"))
    data = json.loads(backend._version_path().read_text())
    assert data["qualities"] == {"hd": "3.8.0", "sd": "3.8.0"}


def test_removing_a_tier_drops_it_from_the_record(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record=["hd", "sd"])
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    backend.remove_other_qualities()
    data = json.loads(backend._version_path().read_text())
    assert data["qualities"] == {"sd": "3.8.0"}
    assert not (backend.install_dir() / "Client" / "Content" / "HD").exists()


def test_a_recorded_tier_whose_folder_was_deleted_does_not_count(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("SD",), record=["hd", "sd"])  # HD recorded but gone from disk
    assert backend.launch_extra_args()[-1] == "-krqlv=sd"
    assert backend.status().data["update_available"] is True


def test_a_broken_qualities_record_falls_back_to_the_disk(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record=None)
    path = backend._version_path()
    data = json.loads(path.read_text())
    data["qualities"] = "hd"  # not a list
    path.write_text(json.dumps(data))
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.status().data["update_available"] is False


# ------------------------------------------------------------------ Settings: ask on switch
try:  # pragma: no cover
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    QApplication = None


@pytest.fixture(scope="module")
def qapp():
    if QApplication is None:
        pytest.skip("PyQt6 not importable")
    return QApplication.instance() or QApplication([])


def _page(tmp_path, qapp, monkeypatch, answer):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    asked: list[str] = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "open", lambda *a, **k: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: asked.append(str(a[2])) or answer
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves", wuwa_quality="hd",
        install_dir=str(tmp_path / "wuwa"), prefix=str(tmp_path / "prefix"),
    )
    config.save()
    page = SettingsPage()
    page.load(config, "wuwa")
    QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    requested: list[str] = []
    page.download_requested.connect(requested.append)
    return page, asked, requested


def _switch_to_sd(page):
    editor = page._option_widgets["wuwa_quality"]
    editor.setCurrentIndex(editor.findData("sd"))
    page.save()


def test_switching_quality_asks_and_now_requests_the_download(tmp_path, qapp, monkeypatch):
    page, asked, requested = _page(tmp_path, qapp, monkeypatch, QMessageBox.StandardButton.Yes)
    try:
        _switch_to_sd(page)
        assert asked and "SD" in asked[-1]
        assert requested == ["wuwa"]
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_later_requests_nothing_and_keeps_the_choice(tmp_path, qapp, monkeypatch):
    from nadare.core.config import Config

    page, asked, requested = _page(tmp_path, qapp, monkeypatch, QMessageBox.StandardButton.No)
    try:
        _switch_to_sd(page)
        assert asked and requested == []
        assert Config.load(tmp_path / "config.json").profiles["wuwa"]["wuwa_quality"] == "sd"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_saving_without_a_quality_change_asks_nothing(tmp_path, qapp, monkeypatch):
    page, asked, requested = _page(tmp_path, qapp, monkeypatch, QMessageBox.StandardButton.Yes)
    try:
        page.save()
        assert asked == [] and requested == []
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_main_window_starts_an_update_on_request(tmp_path, qapp, monkeypatch):
    """The request goes through the same confirmation as the Update button."""
    from nadare.gui import main_window as mw

    calls: list[tuple] = []
    fake = SimpleNamespace(
        config=SimpleNamespace(profiles={"wuwa": {}}),
        predownloads=SimpleNamespace(is_busy=lambda p: False),
        _confirm_action=lambda p, op: calls.append(("confirm", p, op)) or True,
        _start=lambda p, op, *a, **k: calls.append(("start", p, op)) or True,
        _show_predownload_error=lambda p, m: calls.append(("predl", p)),
    )
    mw.MainWindow._on_quality_download_requested(fake, "wuwa")
    assert calls == [("confirm", "wuwa", "update"), ("start", "wuwa", "update")]


# ------------------------------------------------------------------ stress round 1 (2026-11-14)
def _marker(backend: WutheringWavesBackend) -> None:
    backend._write_install_marker()


def test_download_leftovers_do_not_make_a_tier_installed(tmp_path):
    """F1: a cancelled download leaves ``*.part`` / ``*.nadare-new`` next to the target."""
    backend = _backend(tmp_path, "uhd")
    _install(backend, tiers=("HD",), record=None)
    uhd = backend.install_dir() / "Client" / "Content" / "UHD"
    uhd.mkdir(parents=True)
    (uhd / "pakchunk1-UHD.pak.part").write_bytes(b"x" * 64)
    (uhd / "pakchunk2-UHD.pak.nadare-new").write_bytes(b"x" * 64)
    (uhd / "pakchunk3-UHD.pak.nadare-new.part").write_bytes(b"x" * 64)
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.other_quality_folders() == []


def test_an_interrupted_update_with_an_old_record_does_not_trust_the_chosen_tier(tmp_path):
    """W1/W3: old version file, Update to UHD cancelled after real files, HD complete."""
    backend = _backend(tmp_path, "uhd")
    _install(backend, tiers=("HD", "UHD"), record=None)
    _marker(backend)
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.other_quality_folders() == []


def test_the_cleanup_never_deletes_the_tier_that_plays_after_an_interrupted_update(
    tmp_path, monkeypatch
):
    backend = _backend(tmp_path, "uhd")
    _install(backend, tiers=("HD", "UHD"), record=None)
    _marker(backend)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    backend.remove_other_qualities()
    assert (backend.install_dir() / "Client" / "Content" / "HD").is_dir()


def test_recorded_tiers_all_gone_refuses_to_launch_with_repair(tmp_path):
    """W2: the record says tiers were installed, none is on disk: say Repair, do not crash."""
    from nadare.core.backends import BackendError

    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=(), record=["sd"])
    with pytest.raises(BackendError, match="Repair"):
        backend.launch_extra_args()
    status = backend.status()
    assert status.data["update_available"] is True and status.data["pending_quality"] == "sd"


def test_the_pending_update_names_the_setting(tmp_path):
    """A profile set to SD by the old bug: the page says where the choice comes from."""
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD",), record=None)
    assert "Settings" in str(backend.status().data["latest_version"])


# ------------------------------------------------------------------ stress round 1, s3 disjoints
def test_an_other_tier_with_no_files_is_not_offered_for_deletion(tmp_path):
    """s3 F3: a 0-byte leftover HD folder is not "files no longer needed" (0 B prompt)."""
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("SD",), record=["sd"])
    hd = backend.install_dir() / "Client" / "Content" / "HD"
    hd.mkdir(parents=True)
    (hd / "placeholder.pak").write_bytes(b"")
    assert backend.other_quality_folders() == []


@pytest.mark.parametrize(("saved", "tier"), [("UHD", "uhd"), (" sd ", "sd"), ("Hd", "hd")])
def test_the_space_check_reads_the_quality_like_the_backend(tmp_path, monkeypatch, saved, tier):
    """s3 F4: a hand-edited "UHD" was sized as HD (about 20 GiB short)."""
    from nadare.core import diskspace

    backend = _backend(tmp_path, saved)
    monkeypatch.setattr(diskspace, "check_space", lambda *a, **k: None)
    size, _path = diskspace.install_space(backend)
    assert size == backend.install_choice_sizes[tier]


def test_the_game_page_says_when_the_chosen_quality_is_not_downloaded(qapp):
    """s3 F2: the page said "Resource quality: SD" while the game started HD."""
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    page = GameView()
    try:
        page.set_profile("w", {"backend": "direct-wuwa", "title": "WuWa", "wuwa_quality": "sd"})
        page.set_status(Status(state="installed", version="3.8.0",
                               data={"update_available": True, "pending_quality": "sd",
                                     "latest_version": "SD files"}))
        assert "not downloaded" in page.choice_label.text()
        page.set_status(Status(state="installed", version="3.8.0",
                               data={"update_available": False}))
        assert page.choice_label.text() == "Resource quality: SD"
    finally:
        page.deleteLater()
        qapp.processEvents()
