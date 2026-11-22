# SPDX-License-Identifier: GPL-3.0-or-later
"""#1605: one pre-install sheet, and an unofficial notice accepted once for the app.

DNA used to ask three times before its first download (account notice, unofficial box, size
box); every other Store game asked twice. Now one sheet says all of it, and the unofficial
text is remembered for the whole app rather than per game type.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from nadare.core import backends, diskspace
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.notice import AccountNoticeDialog
from nadare.gui.quick_install import QuickInstallDialog, _reflow

DNA = "direct-dna"  # unofficial notice + an account notice + a multi-GB download
PLAIN = "stellasora-yostar"  # unofficial notice + a multi-GB download


def _text(dialog: QuickInstallDialog) -> str:
    return "\n".join(label.text() for label in dialog.findChildren(QLabel))


@pytest.fixture
def sheet(tmp_path, monkeypatch):
    """A window whose fresh installs show one stubbed sheet and start nothing real."""
    app = QApplication.instance() or QApplication([])
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("dna", backend=DNA, title="Duet Night Abyss")
    cfg.add_profile("plain", backend=PLAIN, title="Stella Sora")
    win = MainWindow(cfg)
    # No status yet: a profile added to an empty library is a fresh install.
    monkeypatch.setattr(win, "_snapshot", lambda profile: {})
    monkeypatch.setattr(diskspace, "install_space", lambda backend: (2 << 30, tmp_path))
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 100 << 30)
    dialogs: list[QuickInstallDialog] = []
    starts: list[tuple[str, str]] = []
    answer = [True]

    def execute(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Accepted if answer[0] else QDialog.DialogCode.Rejected

    monkeypatch.setattr(QuickInstallDialog, "exec", execute)
    monkeypatch.setattr(AccountNoticeDialog, "exec", lambda self: pytest.fail("extra account box"))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("extra message box"))
    monkeypatch.setattr(
        win,
        "_start",
        lambda p, op, *a, **kw: (starts.append((p, op)) if op == "install" else None) or True,
    )
    yield win, cfg, dialogs, starts, answer
    win.close()
    win.deleteLater()
    app.processEvents()


def test_dna_first_install_is_one_sheet_with_all_three_parts(sheet):
    win, cfg, dialogs, starts, _ = sheet
    win._current_profile = "dna"
    win._on_game_action("install")

    assert len(dialogs) == 1
    dialog = dialogs[0]
    text = _text(dialog)
    assert dialog.windowTitle() == "Install Duet Night Abyss"
    assert "2 GiB" in text  # the download size
    assert "100 GiB free" in text  # free space on the target
    assert "Nadare is an unofficial launcher" in text
    assert _reflow(backends.get(DNA).account_notice) in text
    assert dialog.install_button.text() == "Install"
    assert dialog.install_button.isDefault()
    assert dialog.cancel_button.text() == "Cancel"
    assert dialog.change_button.isHidden()  # the page fixes the folder

    saved = Config.load(cfg.path)
    assert saved.profile("dna")["accept_account_notice"] is True
    assert saved.data["unofficial_notice_ack"] is True
    assert starts == [("dna", "install")]


def test_a_second_game_type_after_accepting_has_no_unofficial_text(sheet):
    win, cfg, dialogs, starts, _ = sheet
    win._current_profile = "dna"
    win._on_game_action("install")
    win._current_profile = "plain"
    win._on_game_action("install")

    assert len(dialogs) == 2
    second = dialogs[1]
    assert second.notice_text == ""
    assert "Nadare is an unofficial launcher" not in _text(second)
    assert "Download: about 2 GiB." in second.summary_text
    assert second.install_button.isDefault()
    assert starts == [("dna", "install"), ("plain", "install")]


def test_store_dna_uses_one_sheet_and_records_both_acceptances(sheet):
    win, cfg, dialogs, starts, _ = sheet
    cfg.remove_profile("dna")
    win._store_install(DNA)

    assert len(dialogs) == 1
    assert _reflow(backends.get(DNA).account_notice) in _text(dialogs[0])
    profile = next(p for p in cfg.profiles if cfg.profiles[p]["backend"] == DNA)
    saved = Config.load(cfg.path)
    assert saved.profile(profile)["accept_account_notice"] is True
    assert saved.data["unofficial_notice_ack"] is True
    assert starts == [(profile, "install")]


@pytest.mark.parametrize("store", [False, True])
def test_cancel_downloads_nothing_and_records_no_acceptance(sheet, store):
    win, cfg, dialogs, starts, answer = sheet
    answer[0] = False
    if store:
        cfg.remove_profile("dna")
        win._store_install(DNA)
        assert all(p["backend"] != DNA for p in cfg.profiles.values())
    else:
        win._current_profile = "dna"
        win._on_game_action("install")
        assert not cfg.profile("dna").get("accept_account_notice")

    assert len(dialogs) == 1 and starts == []
    assert not cfg.data.get("unofficial_notice_ack")


def test_a_small_download_with_nothing_to_say_shows_no_sheet(sheet, monkeypatch):
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(diskspace, "install_space", lambda backend: (100, "/tmp"))
    monkeypatch.setattr(backends, "install_size_hint", lambda kind: 100)

    win._current_profile = "plain"
    win._on_game_action("install")

    assert dialogs == []
    assert starts == [("plain", "install")]


def test_a_small_store_install_keeps_its_one_sheet(sheet, monkeypatch):
    """The Store sheet also picks the library and version, so it is not dropped (#1536)."""
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(backends, "install_size_hint", lambda kind: 100)
    cfg.remove_profile("plain")

    win._store_install(PLAIN)

    assert len(dialogs) == 1
    assert dialogs[0].notice_text == "" and dialogs[0].account_notice_text == ""
    assert dialogs[0].library_combo is not None
    assert starts and starts[0][1] == "install"


@pytest.mark.parametrize("operation", ["install", "login", "launch"])
def test_an_old_per_game_acceptance_counts_for_the_whole_app(sheet, operation):
    win, cfg, dialogs, _, _ = sheet
    cfg.data["unofficial_notice_ack"] = ["direct-dna"]  # what older versions wrote
    cfg.save()

    assert win._confirm_action("plain", operation)

    assert len(dialogs) == (1 if operation == "install" else 0)
    if dialogs:
        assert dialogs[0].notice_text == ""
    assert Config.load(cfg.path).data["unofficial_notice_ack"] is True


@pytest.mark.parametrize(
    "status",
    [
        SimpleNamespace(state="installed", data={}),
        SimpleNamespace(state="not_installed", data={"incomplete": True}),
    ],
)
def test_an_installed_or_resuming_game_shows_no_sheet(sheet, monkeypatch, status):
    win, cfg, dialogs, _, _ = sheet
    cfg.profile("dna")["accept_account_notice"] = True
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(win, "_snapshot", lambda profile: {"status": status})

    assert win._confirm_action("dna", "install")
    assert dialogs == []


def test_failed_unofficial_save_keeps_the_previous_acceptance(sheet, monkeypatch):
    win, cfg, _, _, _ = sheet
    cfg.data["unofficial_notice_ack"] = []
    monkeypatch.setattr(cfg, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)

    win._remember_unofficial()

    assert cfg.data["unofficial_notice_ack"] == []
    assert not win._unofficial_accepted()


def test_store_account_save_failure_does_not_start_install(sheet, monkeypatch):
    win, cfg, _, starts, _ = sheet
    cfg.remove_profile("dna")
    save = cfg.save

    def save_unless_accepting_account():
        if any(p.get("accept_account_notice") for p in cfg.profiles.values()):
            raise OSError("disk full")
        save()

    monkeypatch.setattr(cfg, "save", save_unless_accepting_account)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    win._store_install(DNA)

    assert starts == []
    assert not cfg.data.get("unofficial_notice_ack")


def test_dna_sheet_fits_a_short_screen(sheet, monkeypatch):
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QScreen
    from PyQt6.QtWidgets import QScrollArea

    win, _, dialogs, _, _ = sheet
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1024, 600))
    win._confirm_download("dna")
    dialog = dialogs[0]
    dialog.show()
    QApplication.processEvents()
    try:
        assert dialog.height() <= 560
        area = dialog.findChild(QScrollArea)
        assert area is not None
        assert area.verticalScrollBar().maximum() > 0
        assert dialog.install_button.isVisible()
        bottom = dialog.install_button.mapTo(dialog, dialog.install_button.rect().bottomRight())
        assert bottom.y() < 560
    finally:
        dialog.hide()


def test_the_account_notice_flows_as_paragraphs_in_the_wide_sheet(qapp):
    """Notices are hard-wrapped for the old fixed-width box; the sheet reflows them."""
    dialog = QuickInstallDialog(
        "Game",
        notice="",
        describe=lambda _f: ("/games/x", "", ""),
        account_notice="It may be\nagainst the terms.\n\nSecond\nparagraph.",
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "It may be against the terms.\n\nSecond paragraph." in texts
    dialog.deleteLater()


def test_reflow_keeps_list_items_on_their_own_lines():
    text = "Steps:\n- one\n  more\n- two\n1. three"
    assert _reflow(text) == "Steps:\n- one more\n- two\n1. three"
