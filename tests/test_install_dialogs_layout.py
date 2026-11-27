# SPDX-License-Identifier: GPL-3.0-or-later
"""Install and notice dialogs never clip, overlap or overflow a 1280x720 screen."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - skipped only when PyQt6 is missing
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont, QScreen
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QLabel,
        QPushButton,
        QScrollArea,
        QWidget,
    )
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import backends as core_backends
from nadare.core.storecatalog import game_of
from nadare.gui import theme
from nadare.gui.notice import AccountNoticeDialog, HoldDialog
from nadare.gui.quick_install import QuickInstallDialog, facts_for
from nadare.gui.runners_dialog import ManageRunnersDialog

LONG_GAMES = [
    ("direct-bpsr", "Blue Protocol: Star Resonance"),
    ("direct-tof", "Tower of Fantasy"),
    ("direct-dna", "Duet Night Abyss"),
    ("launcher-nikke", "GODDESS OF VICTORY: NIKKE"),
    ("direct-endfield", "Arknights: Endfield"),
]

TEXT_SIZES = (13, 17, 20)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def text_size_env(qapp):
    """Apply a desktop font size + real stylesheet, then restore the app font/sheet."""
    saved_font = QFont(qapp.font())
    saved_sheet = qapp.styleSheet()

    def apply(px: int) -> None:
        font = QFont(saved_font)
        font.setPixelSize(px)
        qapp.setFont(font)
        theme.apply_theme(qapp)

    yield apply
    qapp.setFont(saved_font)
    qapp.setStyleSheet(saved_sheet)


def _account_notice(kind: str) -> str:
    notice = getattr(core_backends.get(kind), "account_notice", None)
    return str(notice) if notice else ""


def _make_confirm_dialog(kind: str, title: str) -> QuickInstallDialog:
    return QuickInstallDialog(
        title,
        notice=core_backends.UNOFFICIAL_NOTICE,
        account_notice=_account_notice(kind),
        describe=lambda _chosen: (
            f"/home/player/.local/share/nadare/profiles/{kind}/install",
            "300 GiB free",
            "Download: about 40 GiB. Keep about 85 GiB free while it installs.",
        ),
        change_location=False,
    )


def _make_store_dialog(kind: str, title: str) -> QuickInstallDialog:
    group = game_of(kind)
    builds = group.entries if group is not None else ()
    return QuickInstallDialog(
        title,
        notice=core_backends.UNOFFICIAL_NOTICE,
        account_notice=_account_notice(kind),
        describe=lambda _chosen: (
            f"/home/player/.local/share/nadare/profiles/{kind}/install",
            "97 GiB free",
            (
                f"Installing {title} downloads about 40 GiB and needs about 85 GiB "
                "free space (97 GiB free)."
            ),
        ),
        libraries=[("Default library", "")],
        facts=facts_for(kind),
        variants=[(b.variant or b.title, b.kind) for b in builds],
        current_kind=kind,
    )


def _scroll_ancestor(widget: QWidget, root: QWidget) -> QScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None and parent is not root:
        if isinstance(parent, QScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def _assert_dialog_layout_clean(
    dialog: QDialog,
    buttons: tuple[QPushButton, ...],
    *,
    shown: bool = True,
    max_size: tuple[int, int] | None = None,
    max_width: int | None = None,
    screen_height: int | None = None,
) -> None:
    """Assert no clipped/overlapping labels, buttons stay in view, and dialog fits screen."""
    labels = [
        lb
        for lb in dialog.findChildren(QLabel)
        if lb.text() and (lb.isVisible() if shown else not lb.isHidden())
    ]
    assert labels, "expected at least one visible label"

    for lb in labels:
        width = lb.width()
        height = lb.height()
        assert width > 0 and height > 0, f"degenerate label {lb.text()!r}: {width}x{height}"
        need = lb.heightForWidth(width) if lb.hasHeightForWidth() else lb.sizeHint().height()
        assert need <= height, (
            f"label clipped vertically ({height} < {need}) at width {width}: {lb.text()[:60]!r}"
        )
        if not lb.wordWrap():
            assert lb.sizeHint().width() <= width, (
                f"unwrapped label clipped horizontally ({width} < {lb.sizeHint().width()}): "
                f"{lb.text()[:60]!r}"
            )

    # Check pairwise non-overlap in each coordinate plane (scroll content vs outer dialog).
    groups: dict[int, list[tuple[QLabel, QRect]]] = {}
    for lb in labels:
        area = _scroll_ancestor(lb, dialog)
        plane = area.widget() if area is not None and area.widget() is not None else dialog
        tl = lb.mapTo(plane, lb.rect().topLeft())
        groups.setdefault(id(plane), []).append((lb, QRect(tl, lb.size())))
    for items in groups.values():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                lb_a, rect_a = items[i]
                lb_b, rect_b = items[j]
                assert not rect_a.intersects(rect_b), (
                    f"overlapping labels: {lb_a.text()[:40]!r} ({rect_a}) "
                    f"and {lb_b.text()[:40]!r} ({rect_b})"
                )

    dialog_rect = dialog.rect()
    for btn in buttons:
        if shown:
            assert btn.isVisible(), f"button not visible: {btn.text()!r}"
        else:
            assert not btn.isHidden(), f"button hidden: {btn.text()!r}"
        assert _scroll_ancestor(btn, dialog) is None, (
            f"action button must stay outside QScrollArea: {btn.text()!r}"
        )
        rect = QRect(btn.mapTo(dialog, btn.rect().topLeft()), btn.size())
        assert dialog_rect.contains(rect), (
            f"button outside dialog rect {dialog_rect}: {btn.text()!r} at {rect}"
        )

    if max_size is not None:
        max_w, max_h = max_size
        assert dialog.width() <= max_w, f"dialog wider than screen: {dialog.width()} > {max_w}"
        assert dialog.height() <= max_h, f"dialog taller than screen: {dialog.height()} > {max_h}"
    if max_width is not None:
        assert dialog.width() <= max_width, (
            f"dialog stretched wider than {max_width}px: {dialog.width()}"
        )
    # If the dialog did not reach the screen-height cap, its body must show in full
    # without an unnecessary scrollbar (catches dialogs sized too short for their wrapped text).
    area = dialog.findChild(QScrollArea)
    if area is not None and screen_height is not None and dialog.height() < screen_height - 70:
        assert area.verticalScrollBar().maximum() == 0, (
            f"unnecessary scrollbar (max={area.verticalScrollBar().maximum()}) "
            f"on {dialog.width()}x{dialog.height()} dialog that fits {screen_height}px screen"
        )


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("kind,title", LONG_GAMES)
@pytest.mark.parametrize("mode", ["confirm", "store"])
def test_install_dialogs_never_clip_or_overlap_on_1280x720(
    qapp, text_size_env, monkeypatch, kind, title, px, mode
):
    text_size_env(px)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    dialog = (
        _make_confirm_dialog(kind, title) if mode == "confirm" else _make_store_dialog(kind, title)
    )
    try:
        dialog.show()
        qapp.processEvents()
        _assert_dialog_layout_clean(
            dialog,
            (dialog.install_button, dialog.cancel_button),
            shown=True,
            max_size=(1280, 720),
            max_width=600,
            screen_height=720,
        )
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("kind,title", LONG_GAMES)
@pytest.mark.parametrize("mode", ["confirm", "store"])
def test_install_dialogs_do_not_clip_at_default_size_before_show(
    qapp, text_size_env, kind, title, px, mode
):
    """Grabbing/measuring before showEvent must not render the squeezed 533x288 / 533x533 layout."""
    text_size_env(px)
    dialog = (
        _make_confirm_dialog(kind, title) if mode == "confirm" else _make_store_dialog(kind, title)
    )
    try:
        dialog.grab()
        screen_h = qapp.primaryScreen().availableGeometry().height()
        _assert_dialog_layout_clean(
            dialog,
            (dialog.install_button, dialog.cancel_button),
            shown=False,
            max_width=600,
            screen_height=screen_h,
        )
    finally:
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize(
    "kind,title",
    [
        ("direct-bpsr", "Blue Protocol: Star Resonance"),
        ("launcher-nikke", "GODDESS OF VICTORY: NIKKE"),
        ("direct-tof", "Tower of Fantasy"),
    ],
)
def test_tall_store_dialog_scrolls_content_while_keeping_buttons_fixed(
    qapp, text_size_env, monkeypatch, kind, title
):
    text_size_env(20)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    dialog = _make_store_dialog(kind, title)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.height() <= 720
        area = dialog.findChild(QScrollArea)
        assert area is not None
        assert area.verticalScrollBar().maximum() > 0
        _assert_dialog_layout_clean(
            dialog,
            (dialog.install_button, dialog.cancel_button),
            shown=True,
            max_size=(1280, 720),
            max_width=600,
            screen_height=720,
        )
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("kind", ["direct-dna", "fgo-lepton"])
def test_account_notice_dialog_never_clips_and_fits_1280x720(
    qapp, text_size_env, monkeypatch, kind, px
):
    text_size_env(px)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    dialog = AccountNoticeDialog(_account_notice(kind))
    try:
        dialog.show()
        qapp.processEvents()
        _assert_dialog_layout_clean(
            dialog,
            (dialog.continue_button, dialog.cancel_button),
            shown=True,
            max_size=(1280, 720),
            screen_height=720,
        )
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
def test_hold_dialog_never_clips_and_fits_1280x720(qapp, text_size_env, monkeypatch, px):
    text_size_env(px)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    hold = SimpleNamespace(
        reason=_account_notice("direct-dna"),
        link="https://example.invalid/hold",
    )
    dialog = HoldDialog(hold)
    try:
        dialog.show()
        qapp.processEvents()
        _assert_dialog_layout_clean(
            dialog,
            (dialog.start_button, dialog.cancel_button),
            shown=True,
            max_size=(1280, 720),
            screen_height=720,
        )
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
def test_manage_runners_dialog_never_clips_labels(qapp, text_size_env, monkeypatch, tmp_path, px):
    from nadare.core import runners

    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    text_size_env(px)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    dialog = ManageRunnersDialog({})
    try:
        dialog.show()
        qapp.processEvents()
        buttons = tuple(b for b in dialog.findChildren(QPushButton) if b.isVisible())
        _assert_dialog_layout_clean(dialog, buttons, shown=True, max_size=(1280, 720))
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_store_dialog_on_tall_screen_shows_all_text_without_scrollbar(
    qapp, text_size_env, monkeypatch
):
    text_size_env(13)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1920, 1080))
    dialog = _make_store_dialog("direct-bpsr", "Blue Protocol: Star Resonance")
    try:
        dialog.show()
        qapp.processEvents()
        area = dialog.findChild(QScrollArea)
        assert area is not None
        assert area.verticalScrollBar().maximum() == 0
        _assert_dialog_layout_clean(
            dialog,
            (dialog.install_button, dialog.cancel_button),
            shown=True,
            max_size=(1920, 1080),
        )
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", (13, 20))
@pytest.mark.parametrize(
    "kind,title",
    [
        ("direct-bpsr", "Blue Protocol: Star Resonance"),
        ("direct-dna", "Duet Night Abyss"),
    ],
)
def test_confirm_download_flow_never_clips_or_overlaps(
    tmp_path, qapp, text_size_env, monkeypatch, kind, title, px
):
    from nadare.core import diskspace
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    text_size_env(px)
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("game", backend=kind, title=title)
    win = MainWindow(cfg)
    monkeypatch.setattr(win, "_snapshot", lambda _profile: {})
    monkeypatch.setattr(diskspace, "install_space", lambda _backend: (40 << 30, tmp_path))
    monkeypatch.setattr(diskspace, "free_bytes", lambda _path: 300 << 30)
    checked: list[bool] = []

    def inspect_exec(dialog: QuickInstallDialog) -> int:
        dialog.show()
        qapp.processEvents()
        _assert_dialog_layout_clean(
            dialog,
            (dialog.install_button, dialog.cancel_button),
            shown=True,
            max_size=(1280, 720),
            max_width=600,
            screen_height=720,
        )
        checked.append(True)
        dialog.hide()
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QuickInstallDialog, "exec", inspect_exec)
    try:
        win._confirm_download("game")
        assert checked == [True]
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
