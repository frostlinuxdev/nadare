# SPDX-License-Identifier: GPL-3.0-or-later
"""#1335 stress round 2 (2026-11-14): the completed-tier record, hardened.

Findings from Flash workers r1 (break on purpose), r2 (player journeys), r3 (disjoints):
A. r3 F2: an update of one tier stamped the other, older tier as current.
B. r2: an old version file + a cancelled tier download: the next sync recorded the partial tier.
C. r1: a sync recorded the current setting even with no files on disk.
D. r1: an empty / unknown ``qualities`` record switched off both safety nets.
E. r1/r3 F1: the cleanup was offered while the install-incomplete marker exists.
F. r2: a cancelled quality download read "update to 3.8.0 was interrupted".
G. r2: switching back to the installed tier asked to download it.
H. r2: a refused cleanup said "Removed the HD files".
I. r3 F3: the Kuro tidy could delete files of the tier that plays while another is pending.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from test_wuwa_pending_tier import _backend, _install, _rebind, _tier


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _record(backend) -> dict:
    return json.loads(backend._version_path().read_text())


def _set_record(backend, qualities) -> None:
    path = backend._version_path()
    data = json.loads(path.read_text())
    data["qualities"] = qualities
    path.write_text(json.dumps(data))


def _plan(version="3.8.0"):
    return SimpleNamespace(version=version, game_tag="wuwa")


# ------------------------------------------------------------------ A: per-tier versions
def test_an_update_of_one_tier_leaves_the_other_at_its_own_version(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD", "SD"), record=None)
    _set_record(backend, {"hd": "3.8.0", "sd": "3.8.0"})
    backend._record_synced_version(_plan("3.8.1"))  # an HD update to 3.8.1
    assert _record(backend)["qualities"] == {"hd": "3.8.1", "sd": "3.8.0"}
    _rebind(backend, "sd")
    status = backend.status()
    assert status.data["update_available"] is True and status.data["pending_quality"] == "sd"
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"
    assert backend.other_quality_folders() == []  # SD is not current: HD must stay


def test_a_list_record_from_the_first_build_means_the_recorded_version(tmp_path):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record=["hd", "sd"])
    assert set(backend._complete_tiers()) == {"hd", "sd"}


# ------------------------------------------------------------------ B: record before a sync
def test_the_first_sync_writes_the_record_from_the_disk_before_downloading(tmp_path):
    backend = _backend(tmp_path, "uhd")
    _install(backend, tiers=("HD",), record=None)
    backend._before_sync()
    assert _record(backend)["qualities"] == {"hd": "3.8.0"}


def test_a_cancelled_tier_download_is_never_recorded_by_the_next_sync(tmp_path):
    """r2 journey 3: old record, UHD started and cancelled, back to HD, RESUME finishes."""
    backend = _backend(tmp_path, "uhd")
    _install(backend, tiers=("HD",), record=None)
    backend._before_sync()  # Update to UHD starts...
    _tier(backend, "UHD", size=4096)  # ...writes real files, then is cancelled
    _rebind(backend, "hd")
    backend._before_sync()  # RESUME
    backend._record_synced_version(_plan())
    assert _record(backend)["qualities"] == {"hd": "3.8.0"}
    _rebind(backend, "uhd")
    assert backend.status().data["update_available"] is True
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"


def test_sync_direct_writes_the_record_first(tmp_path, monkeypatch):
    backend = _backend(tmp_path, "hd")
    seen: list[str] = []
    monkeypatch.setattr(backend, "_before_sync", lambda: seen.append("before"))
    monkeypatch.setattr(backend, "_refuse_if_running", lambda what: None)

    def stop():
        seen.append("manifest")
        raise RuntimeError("stop here")

    monkeypatch.setattr(backend, "_load_manifest", stop)
    with pytest.raises(RuntimeError):
        backend._sync_direct()
    assert seen[:2] == ["before", "manifest"]


# ------------------------------------------------------------------ C: only tiers on disk
def test_a_sync_does_not_record_a_chosen_tier_with_no_files(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record={"hd": "3.8.0"})
    _rebind(backend, "sd")  # setting changed, but the sync brought no SD files
    backend._record_synced_version(_plan())
    assert _record(backend)["qualities"] == {"hd": "3.8.0"}


# ------------------------------------------------------------------ D: unusable records
@pytest.mark.parametrize("bad", [[], ["ultra"], {}, {"ultra": "3.8.0"}, 7, "hd"])
def test_an_unusable_record_reads_the_disk_like_an_old_one(tmp_path, bad):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD",), record=None)
    _set_record(backend, bad)
    assert backend.status().data["update_available"] is True
    assert backend.launch_extra_args()[-1] == "-krqlv=hd"


# ------------------------------------------------------------------ E: no cleanup mid-sync
def test_no_cleanup_is_offered_while_a_sync_is_unfinished(tmp_path):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record={"hd": "3.8.0", "sd": "3.8.0"})
    backend._write_install_marker()
    assert backend.other_quality_folders() == []


# ------------------------------------------------------------------ F: the RESUME message
def test_a_cancelled_quality_download_says_so(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record={"hd": "3.8.0"})
    _rebind(backend, "sd")
    backend._write_install_marker(("3.8.0", "3.8.0"))
    status = backend.status()
    assert status.data.get("pending_quality") == "sd"
    assert "SD" in status.message and "RESUME" in status.message
    assert "update to 3.8.0" not in status.message


# ------------------------------------------------------------------ I: the Kuro tidy
def test_the_tidy_waits_while_a_tier_is_pending(tmp_path):
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record={"hd": "3.8.0"})
    _rebind(backend, "sd")
    assert backend._tidy_allowed() is False
    _rebind(backend, "hd")
    assert backend._tidy_allowed() is True


def test_the_tidy_never_touches_another_tier_folder(tmp_path):
    backend = _backend(tmp_path, "sd")
    _install(backend, tiers=("HD", "SD"), record={"hd": "3.8.0", "sd": "3.8.0"})
    names = [
        "Client/Content/HD/pakchunk1-HD-WindowsNoEditor.pak",
        "Client/Content/SD/old-SD.pak",
        "Client/Content/Paks/old.pak",
        "client/content/hd/other.pak",
    ]
    assert backend._tidy_names(names) == ["Client/Content/SD/old-SD.pak",
                                          "Client/Content/Paks/old.pak"]


def test_the_tidy_uses_the_hooks(tmp_path, monkeypatch):
    """The generic tidy asks the backend before deleting and filters what it deletes."""
    backend = _backend(tmp_path, "hd")
    _install(backend, tiers=("HD",), record={"hd": "3.8.0"})
    monkeypatch.setattr(backend, "_tidy_allowed", lambda: False)
    called: list = []
    monkeypatch.setattr(backend, "_load_manifest", lambda: called.append(1))
    backend._tidy_retired_once()
    assert called == []


# ------------------------------------------------------------------ G and H: GUI
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


def test_switching_back_to_the_installed_tier_asks_nothing(tmp_path, qapp, monkeypatch):
    """r2 journey 1: a profile set to SD by #1332, only HD on disk; the player sets HD back."""
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    install = tmp_path / "wuwa"
    asked: list[str] = []
    notices: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or
                        QMessageBox.StandardButton.Yes)
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "open", lambda *a, **k: None)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("w", backend="direct-wuwa", title="W", wuwa_quality="sd",
                       install_dir=str(install), prefix=str(tmp_path / "pfx"))
    config.save()
    real = _backend(tmp_path / "b", "sd")
    real.ctx.profile["install_dir"] = str(install)
    _install(real, tiers=("HD",), record=None)
    page = SettingsPage()
    requested: list[str] = []
    page.download_requested.connect(requested.append)
    page.notice.connect(lambda p, t: notices.append(t))
    try:
        page.load(config, "w")
        QThreadPool.globalInstance().waitForDone(5000)
        qapp.processEvents()
        combo = page._option_widgets["wuwa_quality"]
        combo.setCurrentIndex(combo.findData("hd"))
        page.save()
        assert asked == [] and requested == []
        assert not any("downloaded by Update" in t for t in notices)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_refused_cleanup_does_not_claim_files_were_removed(qapp):
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    lines: list[str] = []
    fake = SimpleNamespace(
        config=SimpleNamespace(profiles={"w": {}}),
        _current_profile="w",
        _set_side_status=lines.append,
        game=SimpleNamespace(append_line=lambda text, kind: None),
        _start=lambda *a, **k: True,
    )
    CleanupMixin._quality_cleanup_finished(fake, "w", [("hd", "x", 100)], (0, []))
    assert lines and "Removed" not in lines[-1]
