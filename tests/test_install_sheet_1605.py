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
    # The sheet's "Download" line is the download figure, not the space check's.
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 2 << 30)
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
    assert "Download: about 2 GiB." in text  # the download size, not the space stub
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
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 100)

    win._current_profile = "plain"
    win._on_game_action("install")

    assert dialogs == []
    assert starts == [("plain", "install")]


def test_a_small_store_install_keeps_its_one_sheet(sheet, monkeypatch):
    """The Store sheet also picks the library and version, so it is not dropped (#1536)."""
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(backends, "install_size_hint", lambda kind: 100)
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 100)
    cfg.remove_profile("plain")

    win._store_install(PLAIN)

    assert len(dialogs) == 1
    assert dialogs[0].notice_text == "" and dialogs[0].account_notice_text == ""
    assert dialogs[0].library_combo is not None
    assert "100 B" in dialogs[0].summary_text
    assert "2 GiB" not in dialogs[0].summary_text
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


def _use_bpsr_hints(monkeypatch):
    from nadare.backends.direct import BpsrBackend

    monkeypatch.setattr(
        backends, "download_size_hint", lambda kind: BpsrBackend.download_size_hint
    )
    monkeypatch.setattr(
        backends, "install_space_hint", lambda kind: BpsrBackend.install_space_hint
    )
    return BpsrBackend


def test_a_space_failure_still_explains_the_peak(sheet, monkeypatch, tmp_path):
    """BPSR with 60 GiB free: the check fails, but the sheet still says the peak (with margin)."""
    from nadare.core.backends import BackendError
    from nadare.core.diskspace import human_size

    win, cfg, dialogs, starts, _ = sheet
    cfg.add_profile("bpsr", backend="direct-bpsr", title="Blue Protocol: Star Resonance")
    peak_hint = _use_bpsr_hints(monkeypatch)

    def too_little(backend):
        raise BackendError("Not enough disk space on /: need 95 GiB, 60 GiB free", code=4)

    monkeypatch.setattr(diskspace, "install_space", too_little)
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 60 << 30)

    win._current_profile = "bpsr"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert f"Download: about {human_size(peak_hint.download_size_hint)}." in text
    assert (
        f"Keep about {human_size(diskspace.free_needed(peak_hint.install_space_hint))} free"
        " while it installs." in text
    )
    assert "60 GiB free" in text
    assert not dialogs[0].install_button.isEnabled()
    assert starts == []


def test_a_smaller_space_figure_does_not_get_the_keep_sentence(sheet, monkeypatch, tmp_path):
    """Only a *larger* peak is worth a second sentence (same guard as the Store sheet)."""
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 2 << 30)
    monkeypatch.setattr(backends, "install_space_hint", lambda kind: 1 << 30)
    monkeypatch.setattr(diskspace, "install_space", lambda backend: (1 << 30, tmp_path))

    win._current_profile = "plain"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert "Download: about 2 GiB." in text
    assert "Keep about" not in text
    assert starts == [("plain", "install")]


def test_a_peak_that_formats_the_same_does_not_get_the_keep_sentence(
    sheet, monkeypatch, tmp_path
):
    """One byte more is not a different figure once formatted; do not repeat it."""
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 2 << 30)
    monkeypatch.setattr(backends, "install_space_hint", lambda kind: (2 << 30) + 1)
    monkeypatch.setattr(diskspace, "install_space", lambda backend: ((2 << 30) + 1, tmp_path))

    win._current_profile = "plain"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert "Download: about 2 GiB." in text
    assert "Keep about" not in text


def test_a_big_peak_still_asks_even_when_the_download_is_small(sheet, monkeypatch, tmp_path):
    """The download is under the threshold but its unpacking peak is not: still ask."""
    win, cfg, dialogs, starts, _ = sheet
    cfg.data["unofficial_notice_ack"] = True
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 100)
    monkeypatch.setattr(backends, "install_space_hint", lambda kind: 2 << 30)
    monkeypatch.setattr(diskspace, "install_space", lambda backend: (2 << 30, tmp_path))

    win._current_profile = "plain"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert "Download: about 100 B." in text
    keep = diskspace.human_size(diskspace.free_needed(2 << 30))
    assert f"Keep about {keep} free while it installs." in text


def test_bpsr_sheet_names_the_download_and_the_peak(sheet, monkeypatch, tmp_path):
    """BPSR's download and its unpacking peak differ, so the sheet says both."""
    from nadare.core.diskspace import human_size

    win, cfg, dialogs, starts, _ = sheet
    cfg.add_profile("bpsr", backend="direct-bpsr", title="Blue Protocol: Star Resonance")
    cfg.data["unofficial_notice_ack"] = True
    bpsr = _use_bpsr_hints(monkeypatch)
    monkeypatch.setattr(
        diskspace, "install_space", lambda backend: (bpsr.install_space_hint, tmp_path)
    )

    win._current_profile = "bpsr"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert f"Download: about {human_size(bpsr.download_size_hint)}." in text
    keep = human_size(diskspace.free_needed(bpsr.install_space_hint))
    assert f"Keep about {keep} free while it installs." in text
    assert starts == [("bpsr", "install")]


def test_wuwa_sheet_download_is_the_chosen_quality_not_the_generic_hint(
    sheet, monkeypatch, tmp_path
):
    """An install-choice game downloads its picked quality, not the generic size."""
    from nadare.core.diskspace import human_size

    win, cfg, dialogs, starts, _ = sheet
    cfg.add_profile("wuwa", backend="direct-wuwa", title="Wuthering Waves")
    cfg.profile("wuwa")["wuwa_quality"] = "uhd"
    chosen = 99 << 30
    # The generic hint would print 80 GiB, so a stale fallback is visible.
    monkeypatch.setattr(backends, "download_size_hint", lambda kind: 80 << 30)
    monkeypatch.setattr(backends, "install_space_hint", lambda kind: 80 << 30)
    monkeypatch.setattr(diskspace, "install_space", lambda backend: (chosen, tmp_path))

    win._current_profile = "wuwa"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    assert f"Download: about {human_size(chosen)}." in text
    assert "Keep about" not in text  # the chosen quality is both the download and the peak
    assert starts == [("wuwa", "install")]


_REAL_INSTALL_SPACE = diskspace.install_space


def test_the_keep_free_figure_is_the_one_the_space_check_needs(sheet, monkeypatch, tmp_path):
    """A real failed check (60 GiB free): the keep-free figure is the error's need (user 11-24)."""
    import re

    win, cfg, dialogs, starts, _ = sheet
    cfg.add_profile("bpsr", backend="direct-bpsr", title="Blue Protocol: Star Resonance")
    _use_bpsr_hints(monkeypatch)
    monkeypatch.setattr(diskspace, "install_space", _REAL_INSTALL_SPACE)
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 60 << 30)
    monkeypatch.setattr(diskspace, "tree_bytes", lambda path, **_: 0)

    win._current_profile = "bpsr"
    win._on_game_action("install")

    assert len(dialogs) == 1
    text = _text(dialogs[0])
    need = re.search(r"need (\S+ \S+),", text)
    keep = re.search(r"Keep about (\S+ \S+) free while it installs\.", text)
    assert need and keep, text
    assert keep.group(1) == need.group(1)
    assert starts == []
