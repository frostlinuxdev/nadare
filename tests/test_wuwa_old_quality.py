# SPDX-License-Identifier: GPL-3.0-or-later
"""#990 point 3: after a WuWa quality change, the old tier's files can be removed.

Each tier lives in its own folder, ``Client/Content/SD|HD|UHD`` (checked against the live index on
2026-11-08: UHD to SD leaves 61.5 GiB, HD to SD 42.6 GiB). After an update or install of the
chosen tier, Nadare offers to remove the other tiers' folders; never while the game runs, never
the chosen tier, never through a symlink or onto another drive.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.direct import WutheringWavesBackend


def _backend(tmp_path: Path, quality: str = "sd") -> WutheringWavesBackend:
    values = {"install_dir": str(tmp_path / "wuwa"), "prefix": str(tmp_path / "prefix"),
              "wuwa_quality": quality}
    return WutheringWavesBackend(make_context(tmp_path, "direct-wuwa", values))


def _tier(backend: WutheringWavesBackend, name: str, size: int) -> Path:
    folder = backend.install_dir() / "Client" / "Content" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"pakchunk1-{name}-WindowsNoEditor.pak").write_bytes(b"x" * size)
    return folder


def test_the_other_tiers_are_listed_with_their_size(tmp_path):
    backend = _backend(tmp_path, "sd")
    _tier(backend, "SD", 10)
    uhd = _tier(backend, "UHD", 300)
    hd = _tier(backend, "HD", 200)
    found = backend.other_quality_folders()
    assert [(q, p, s) for q, p, s in found] == [("hd", hd, 200), ("uhd", uhd, 300)]


def test_nothing_is_offered_until_the_chosen_tier_is_there(tmp_path):
    backend = _backend(tmp_path, "sd")
    _tier(backend, "UHD", 300)  # the switch to SD has not been downloaded yet
    assert backend.other_quality_folders() == []


def test_removing_deletes_only_the_other_tiers(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "sd")
    sd = _tier(backend, "SD", 10)
    uhd = _tier(backend, "UHD", 300)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    freed, warnings = backend.remove_other_qualities()
    assert freed == 300 and warnings == []
    assert not uhd.exists() and (sd / "pakchunk1-SD-WindowsNoEditor.pak").is_file()


def test_a_symlinked_tier_folder_is_never_followed(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "sd")
    _tier(backend, "SD", 10)
    elsewhere = tmp_path / "precious"
    elsewhere.mkdir()
    (elsewhere / "keep.bin").write_bytes(b"k")
    (backend.install_dir() / "Client" / "Content" / "UHD").symlink_to(elsewhere)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    assert backend.other_quality_folders() == []
    backend.remove_other_qualities()
    assert (elsewhere / "keep.bin").is_file()


def test_removing_while_the_game_runs_is_refused(tmp_path, monkeypatch):
    from nadare.core.backends import BackendError

    backend = _backend(tmp_path, "sd")
    _tier(backend, "SD", 10)
    uhd = _tier(backend, "UHD", 300)

    def running(what):
        raise BackendError("Stop the game first")

    monkeypatch.setattr(backend, "_refuse_if_running", running)
    with pytest.raises(BackendError):
        backend.remove_other_qualities()
    assert uhd.is_dir()


# ---- the window offers it after a successful update ---------------------------------------------
def test_the_window_offers_to_remove_and_removes_on_yes(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.profiles["w"] = {"backend": "direct-wuwa", "title": "WuWa",
                            "install_dir": str(tmp_path / "wuwa"), "wuwa_quality": "sd"}
    window = MainWindow(config)
    asked: list[tuple[str, list]] = []
    removed: list[str] = []
    monkeypatch.setattr(window, "_ask_remove_qualities",
                        lambda title, items, current: asked.append((title, items, current)) or True)
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self, only=None: removed.append("yes") or (300, []))
    try:
        window._offer_quality_cleanup("w", [("uhd", tmp_path / "wuwa/Client/Content/UHD", 300)])
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        assert asked and asked[0][0] == "WuWa" and asked[0][2] == "sd"
        assert removed == ["yes"]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_saying_no_removes_nothing(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.profiles["w"] = {"backend": "direct-wuwa", "title": "WuWa",
                            "install_dir": str(tmp_path / "wuwa"), "wuwa_quality": "sd"}
    window = MainWindow(config)
    monkeypatch.setattr(window, "_ask_remove_qualities", lambda title, items, current: False)
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self: pytest.fail("removed without asking"))
    try:
        window._offer_quality_cleanup("w", [("uhd", tmp_path / "x", 300)])
        app.processEvents()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_removing_named_tiers_only_touches_those_tiers(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "sd")
    _tier(backend, "SD", 10)
    hd = _tier(backend, "HD", 200)
    uhd = _tier(backend, "UHD", 300)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    freed, warnings = backend.remove_other_qualities(only=["uhd"])
    assert freed == 300 and warnings == []
    assert not uhd.exists() and hd.is_dir()


def test_the_cleanup_worker_gets_a_snapshot_of_the_settings(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from nadare.core import backends as core_backends
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.profiles["w"] = {"backend": "direct-wuwa", "title": "WuWa",
                            "install_dir": str(tmp_path / "wuwa"), "wuwa_quality": "sd"}
    window = MainWindow(config)
    seen: list[tuple[str, object]] = []

    class _Backend:
        def remove_other_qualities(self, only=None):
            return (0, [])

    def fake_create(profile, worker_config, *args, **kwargs):
        seen.append((profile, worker_config))
        return _Backend()

    monkeypatch.setattr(core_backends, "create", fake_create)
    monkeypatch.setattr(window, "_ask_remove_qualities", lambda *a: True)
    try:
        window._offer_quality_cleanup("w", [("uhd", tmp_path / "wuwa/Client/Content/UHD", 300)])
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        assert seen and seen[-1][0] == "w"
        assert seen[-1][1] is not window.config
        assert seen[-1][1].profiles["w"]["wuwa_quality"] == "sd"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_the_quality_is_read_from_extra_too(tmp_path):
    backend = WutheringWavesBackend(
        make_context(
            tmp_path,
            "direct-wuwa",
            {
                "install_dir": str(tmp_path / "wuwa"),
                "prefix": str(tmp_path / "prefix"),
                "extra": {"wuwa_quality": "sd"},
            },
        )
    )
    assert backend.launch_extra_args()[-1] == "-krqlv=sd"


def test_an_empty_subfolder_does_not_count_as_the_chosen_tier(tmp_path):
    backend = _backend(tmp_path, "sd")
    sd = backend.install_dir() / "Client" / "Content" / "SD"
    (sd / "empty").mkdir(parents=True)
    _tier(backend, "UHD", 300)
    assert backend.other_quality_folders() == []


def test_a_symlinked_content_folder_is_never_followed(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "sd")
    outside = tmp_path / "outside-content"
    outside.mkdir()
    for name, size in (("SD", 10), ("UHD", 300)):
        _tier_at(outside, name, size)
    content = backend.install_dir() / "Client" / "Content"
    content.parent.mkdir(parents=True, exist_ok=True)
    content.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)
    assert backend.other_quality_folders() == []
    backend.remove_other_qualities()
    assert (outside / "UHD" / "pakchunk1-UHD-WindowsNoEditor.pak").is_file()


def _tier_at(root: Path, name: str, size: int) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"pakchunk1-{name}-WindowsNoEditor.pak").write_bytes(b"x" * size)
    return folder


def test_a_symlink_above_the_install_folder_is_fine(tmp_path, monkeypatch):
    """/home -> /var/home on Silverblue/Bazzite: only links below the install dir are refused."""
    real = tmp_path / "var-home"
    real.mkdir()
    (tmp_path / "linkhome").symlink_to(real, target_is_directory=True)
    values = {"install_dir": str(tmp_path / "linkhome" / "wuwa"), "prefix": str(tmp_path / "p"),
              "wuwa_quality": "sd"}
    backend = WutheringWavesBackend(make_context(tmp_path, "direct-wuwa", values))
    _tier(backend, "SD", 10)
    uhd = _tier(backend, "UHD", 300)
    assert [(q, p) for q, p, _s in backend.other_quality_folders()] == [("uhd", uhd)]
