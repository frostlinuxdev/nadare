# SPDX-License-Identifier: GPL-3.0-or-later
"""WuWa crashed with "Fatal error" after a Settings save the player never meant as a quality change.

Found on a real desktop (2026-11-14): a WuWa profile made before #990 had no ``wuwa_quality``.
The Settings page showed the first choice (SD) for the unset value, so saving any other setting
wrote ``sd``. Launch then passed ``-krqlv=sd`` while only ``Client/Content/HD`` was installed,
and the client crashed about 40 s in, every launch.

Two guards:
1. A ``choice`` option with no saved value shows (and saves) its default, not the first entry.
2. WuWa starts with a quality whose files are installed; a tier chosen but not downloaded yet
   falls back to the installed one until Update brings it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context

from nadare.backends.direct import WutheringWavesBackend
from nadare.core.events import Event

try:  # pragma: no cover
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    QApplication = None


# ------------------------------------------------------------------ launch: installed tier only
def _backend(tmp_path: Path, quality: str | None, events: list | None = None):
    values = {"install_dir": str(tmp_path / "wuwa"), "prefix": str(tmp_path / "prefix")}
    if quality is not None:
        values["wuwa_quality"] = quality
    ctx = make_context(tmp_path, "direct-wuwa", values)
    if events is not None:
        ctx.emit = events.append
    return WutheringWavesBackend(ctx)


def _tier(backend: WutheringWavesBackend, name: str) -> Path:
    folder = backend.install_dir() / "Client" / "Content" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"pakchunk1-{name}-WindowsNoEditor.pak").write_bytes(b"x" * 16)
    return folder


def test_a_chosen_tier_that_is_not_downloaded_starts_the_installed_one(tmp_path):
    """The real case: SD chosen (by the bug or by hand), only HD on disk."""
    backend = _backend(tmp_path, "sd")
    _tier(backend, "HD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=hd"]


def test_the_fallback_is_said_in_the_launch_log(tmp_path):
    events: list = []
    backend = _backend(tmp_path, "sd", events)
    _tier(backend, "HD")
    backend.launch_extra_args()
    text = " ".join(e.message for e in events if isinstance(e, Event))
    assert "SD" in text and "HD" in text and "Update" in text


def test_the_chosen_tier_is_used_once_it_is_installed(tmp_path):
    backend = _backend(tmp_path, "sd")
    _tier(backend, "HD")
    _tier(backend, "SD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=sd"]


@pytest.mark.parametrize("quality", ["sd", "hd", "uhd"])
def test_no_tier_folders_at_all_keeps_the_chosen_tier(tmp_path, quality):
    """Nothing to compare with (fresh or unusual layout): behave as before."""
    backend = _backend(tmp_path, quality)
    assert backend.launch_extra_args() == ["-dx12", f"-krqlv={quality}"]


def test_an_empty_chosen_folder_does_not_count_as_installed(tmp_path):
    backend = _backend(tmp_path, "uhd")
    (backend.install_dir() / "Client" / "Content" / "UHD" / "empty").mkdir(parents=True)
    _tier(backend, "HD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=hd"]


def test_a_chosen_folder_with_only_empty_files_does_not_count(tmp_path):
    """A started-but-empty download (0-byte placeholders) is not an installed tier."""
    backend = _backend(tmp_path, "sd")
    sd = backend.install_dir() / "Client" / "Content" / "SD"
    sd.mkdir(parents=True)
    (sd / "pakchunk1-SD-WindowsNoEditor.pak").write_bytes(b"")
    _tier(backend, "HD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=hd"]


def test_with_two_other_tiers_installed_hd_is_preferred(tmp_path):
    backend = _backend(tmp_path, "uhd")
    _tier(backend, "SD")
    _tier(backend, "HD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=hd"]


def test_only_sd_installed_and_uhd_chosen_starts_sd(tmp_path):
    backend = _backend(tmp_path, "uhd")
    _tier(backend, "SD")
    assert backend.launch_extra_args() == ["-dx12", "-krqlv=sd"]


def test_own_launch_args_still_win(tmp_path):
    backend = _backend(tmp_path, "sd")
    backend.ctx.profile["launch_args"] = "-krqlv=uhd"
    _tier(backend, "HD")
    assert backend.launch_extra_args() == []


def test_the_saved_choice_is_not_rewritten_by_the_fallback(tmp_path):
    """The player's choice stays; Update still downloads it."""
    backend = _backend(tmp_path, "sd")
    _tier(backend, "HD")
    backend.launch_extra_args()
    assert backend.ctx.profile["wuwa_quality"] == "sd"
    assert backend.plan_kwargs()["quality"] == "sd"


# ------------------------------------------------------------------ Settings: unset = default


@pytest.fixture(scope="module")
def qapp():
    if QApplication is None:
        pytest.skip("PyQt6 not importable")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page_rig(tmp_path, qapp, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui import settings as settings_mod
    from nadare.gui.settings import SettingsPage

    monkeypatch.setattr(
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="installed", data={})
        ),
        raising=False,
    )
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "open", lambda *a, **k: None)
    config = Config.load(tmp_path / "config.json")
    # A profile from before #990: no wuwa_quality key at all.
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(tmp_path / "wuwa"), prefix=str(tmp_path / "prefix"),
    )
    config.save()
    page = SettingsPage()
    page.load(config, "wuwa")
    QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    yield SimpleNamespace(page=page, config=config, path=tmp_path / "config.json")
    page.deleteLater()
    qapp.processEvents()


def test_an_unset_quality_shows_the_default_hd(page_rig):
    editor = page_rig.page._option_widgets["wuwa_quality"]
    assert editor.currentData() == "hd"


def test_saving_another_setting_never_writes_sd(page_rig):
    from nadare.core.config import Config

    page_rig.page.save()
    saved = Config.load(page_rig.path).profiles["wuwa"]
    assert saved.get("wuwa_quality") in (None, "hd")


def test_a_saved_choice_is_still_shown_as_saved(tmp_path, qapp, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("wuwa", backend="direct-wuwa", title="W", wuwa_quality="uhd")
    page = SettingsPage()
    try:
        page.load(config, "wuwa")
        assert page._option_widgets["wuwa_quality"].currentData() == "uhd"
    finally:
        page.deleteLater()
        qapp.processEvents()
