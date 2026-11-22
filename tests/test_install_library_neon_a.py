# SPDX-License-Identifier: GPL-3.0-or-later
"""Install feedback and metadata/progress consistency regressions."""

from datetime import UTC, datetime

import pytest
from PyQt6.QtWidgets import QDialog

from nadare.core import diskspace
from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui.library import LibraryPage
from nadare.gui.main_window import MainWindow
from nadare.gui.quick_install import QuickInstallDialog


@pytest.fixture
def window(tmp_path, qtbot):
    config = Config.load(tmp_path / "config.json")
    window = MainWindow(config)
    qtbot.addWidget(window)
    yield window
    window.close()


@pytest.mark.parametrize("assume", [True, False])
def test_store_account_notice_requires_acceptance(window, monkeypatch, assume):
    if not assume:
        monkeypatch.delenv("NADARE_ASSUME_NOTICES")
    shown, starts = [], []
    monkeypatch.setattr(
        QuickInstallDialog, "exec",
        lambda dialog: shown.append(dialog) or QDialog.DialogCode.Rejected,
    )
    monkeypatch.setattr(window, "_start", lambda *args, **kwargs: starts.append(args))
    window._store_install("direct-dna")
    assert len(shown) == 1
    assert starts == []
    saved = Config.load(window.config.path)
    assert not any(p.get("accept_account_notice") for p in saved.profiles.values())


def test_small_install_reports_insufficient_space(window, monkeypatch):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES")
    window.config.data["unofficial_notice_ack"] = True
    window.config.add_profile("g", backend="direct-gfl2-haoplay")
    monkeypatch.setattr(window, "_snapshot", lambda profile: {})
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 100 * 1024**2)
    shown, statuses, starts = [], [], []

    def reject(dialog):
        shown.append(dialog)
        assert not dialog.install_button.isEnabled()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QuickInstallDialog, "exec", reject)
    monkeypatch.setattr(window, "_set_side_status", statuses.append)
    monkeypatch.setattr(window, "_start", lambda *args: starts.append(args))
    window._current_profile = "g"
    window._on_game_action("install")
    assert len(shown) == 1
    assert any("space" in status.lower() for status in statuses)
    assert starts == []


def test_new_step_drops_previous_transfer_measurements(window, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("nadare.gui.main_window.time.monotonic", lambda: now[0])
    gib = 1 << 30
    for fraction in (0.5, 1.0):
        window._track_download_progress("g", Event(
            "progress", progress=fraction,
            data={"done": fraction * 50 * gib, "total": 50 * gib},
        ))
        now[0] += 1
    assert "GiB/s" in window._download_progress["g"][2]
    previous_eta = window._download_eta["g"]
    window._download_eta_text["g"] = "about 2 h left"
    window._track_download_progress("g", Event("step", "Downloading WebView2"))
    assert window._download_progress["g"][0] is None
    assert window._download_progress["g"][2] == ""
    for fraction in (0.1, 0.2):
        window._track_download_progress("g", Event("progress", progress=fraction))
        now[0] += 1
    assert "/s" not in window._download_progress["g"][2]
    assert "g" not in window._download_totals
    assert "g" not in window._download_speed
    # The fraction ETA starts with the new phase, not the preceding completed download.
    assert window._download_eta["g"] is not previous_eta


def test_favourite_card_receives_same_metadata_and_seed(qtbot, monkeypatch):
    from nadare.gui.library import GameCard

    constructors = []
    original = GameCard.__init__

    def record(card, *args, **kwargs):
        constructors.append(kwargs)
        original(card, *args, **kwargs)

    monkeypatch.setattr(GameCard, "__init__", record)
    page = LibraryPage()
    qtbot.addWidget(page)
    timestamp = datetime.now(UTC).isoformat()
    page.refresh({"different-slug": {
        "title": "Wuthering Waves", "backend": "direct-wuwa", "favourite": True,
        "added_at": timestamp, "last_played": timestamp,
    }})
    main = page._cards["different-slug"]
    favourite = page._favourite_cards["different-slug"]
    assert favourite.meta_label.text() == main.meta_label.text()
    assert favourite._seed == main._seed == "Wuthering Waves"
    favourite_args = next(args for args in constructors if args.get("favourite"))
    assert favourite_args["last_played"] == timestamp
    assert favourite.new_pill.isHidden()
