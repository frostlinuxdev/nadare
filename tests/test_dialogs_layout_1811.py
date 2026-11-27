# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared layout checks for every dialog moved in #1811, plus width retention on reshow."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - skipped only when PyQt6 is missing
    from PyQt6.QtCore import QPointF, QRect, Qt
    from PyQt6.QtGui import QMouseEvent, QScreen
    from PyQt6.QtWidgets import (
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QPushButton,
        QScrollArea,
        QWidget,
    )
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_install_dialogs_layout import (
    _assert_dialog_layout_clean,
    _scroll_ancestor,
)

from nadare.core import backends as core_backends
from nadare.core import (
    gamefinder,
    libraries,
    profilefolders,
    runners,
    safetycheck,
    steamsetup,
    userns,
)
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog
from nadare.gui.disk_space_panel import ProfileFoldersDialog, RuntimeFoldersDialog
from nadare.gui.found_copies import FoundCopiesDialog, SpareCopiesDialog
from nadare.gui.libraries_panel import LibrariesPanel
from nadare.gui.library_picker import LibraryPicker
from nadare.gui.logs_dialog import LogsDialog
from nadare.gui.notice import AccountNoticeDialog, HoldDialog
from nadare.gui.runners_dialog import ManageRunnersDialog
from nadare.gui.safety_check import SafetyResultDialog
from nadare.gui.steam_setup import SteamSetupDialog
from nadare.gui.support_report import ReportDialog
from nadare.gui.system_check import SystemCheckDialog
from nadare.gui.terms_dialog import TermsDialog
from nadare.gui.userns_dialog import UsernsDialog

pytest_plugins = ("test_install_dialogs_layout",)

TEXT_SIZES = (13, 20)
SCREENS = ((1280, 720), (1024, 600))
EXPECTED_MARGINS = (22, 20, 22, 20)
EXPECTED_SPACING = 14


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(tmp_path / "config.json")
    lib1 = tmp_path / "drives" / "fast-nvme-ssd-games" / "Nadare"
    lib2 = tmp_path / "drives" / "secondary-archive-drive" / "Nadare"
    lib1.mkdir(parents=True, exist_ok=True)
    lib2.mkdir(parents=True, exist_ok=True)
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib1)}, {"path": str(lib2)}]
    cfg.save()
    return cfg


def _make_plan(extra_steps: int = 0) -> steamsetup.SetupPlan:
    items = [
        steamsetup.SetupItem(
            key="installed",
            label="Installed in Steam",
            state=steamsetup.STATE_DONE,
            detail="Found in /home/player/.local/share/Steam/steamapps/common/Wuthering Waves",
        ),
        steamsetup.SetupItem(
            key="runner",
            label="dwproton-11.0-17-x86_64 runner",
            state=steamsetup.STATE_TODO,
            detail="Downloads and selects the tested Proton build for this game in Steam.",
            action="set_runner",
            action_label="Set runner",
        ),
        steamsetup.SetupItem(
            key="prefix:webview2",
            label="Microsoft Edge WebView2",
            state=steamsetup.STATE_RECOMMENDED,
            detail=(
                "Needed for the in-game notices, login web view, and store pages to render "
                "properly on Linux."
            ),
            action="install_prefix:webview2",
            action_label="Install",
        ),
        steamsetup.SetupItem(
            key="wayland",
            label="Native Wayland",
            state=steamsetup.STATE_OPTIONAL,
            detail=(
                "Runs the game directly on Wayland instead of XWayland; can be undone anytime."
            ),
            action="set_wayland",
            action_label="Enable",
        ),
        steamsetup.SetupItem(
            key="h264",
            label="H.264 video playback",
            state=steamsetup.STATE_BLOCKED,
            detail="Requires a Proton runner with media codecs enabled for cutscenes.",
        ),
    ]
    for i in range(extra_steps):
        items.append(
            steamsetup.SetupItem(
                key=f"prefix:extra{i}",
                label=f"Extra runtime component {i + 1}",
                state=steamsetup.STATE_OPTIONAL,
                detail=(
                    "Optional compatibility component with a longer description so the checklist "
                    "grows taller than a small laptop display."
                ),
                action=f"install_prefix:extra{i}",
                action_label="Install",
            )
        )
    return steamsetup.SetupPlan(
        app_id="3513350",
        game="Wuthering Waves",
        verified=True,
        note=(
            "Steam must be restarted after changing the compatibility tool so the new setting "
            "takes effect."
        ),
        items=tuple(items),
    )


def _find_buttons(dialog: QDialog, texts: tuple[str, ...]) -> tuple[QPushButton, ...]:
    found: list[QPushButton] = []
    all_buttons = dialog.findChildren(QPushButton)
    for want in texts:
        match = next((b for b in all_buttons if b.text() == want), None)
        assert match is not None, (
            f"missing button {want!r}; saw {[b.text() for b in all_buttons]!r}"
        )
        found.append(match)
    return tuple(found)


def _rect_in(widget: QWidget, root: QWidget) -> QRect:
    return QRect(widget.mapTo(root, widget.rect().topLeft()), widget.size())


def _assert_shared_layout(
    dialog: QDialog,
    buttons: tuple[QPushButton, ...],
    *,
    screen_size: tuple[int, int],
    scroll_body: bool,
    body_widget: QWidget | None = None,
    expect_heading: bool = True,
) -> None:
    layout = dialog.layout()
    assert layout is not None
    layout.activate()
    margins = layout.contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == EXPECTED_MARGINS
    assert layout.spacing() == EXPECTED_SPACING

    if expect_heading:
        titles = [
            lb
            for lb in dialog.findChildren(QLabel)
            if lb.objectName() == "Title" and lb.isVisible()
        ]
        assert titles, "expected a visible QLabel with objectName 'Title'"
        heading = titles[0]
        assert heading.text().strip(), "heading text must not be empty"
        assert heading.wordWrap(), f"heading must be word-wrapped: {heading.text()!r}"
        assert _scroll_ancestor(heading, dialog) is None, (
            "heading must sit outside the scroll body"
        )

    screen_w, screen_h = screen_size
    _assert_dialog_layout_clean(
        dialog,
        buttons,
        shown=True,
        max_size=(screen_w, screen_h),
        screen_height=screen_h if scroll_body else None,
    )

    if scroll_body:
        area = dialog.findChild(QScrollArea)
        assert area is not None, "expected dialog body inside make_scroll_body()"
        assert area.widget() is not None
        assert area.widget().width() <= area.viewport().width(), (
            f"scroll content ({area.widget().width()}px) wider than viewport "
            f"({area.viewport().width()}px)"
        )
        body = area
    else:
        assert body_widget is not None
        body = body_widget

    body_rect = _rect_in(body, dialog)
    for btn in buttons:
        btn_rect = _rect_in(btn, dialog)
        assert btn_rect.top() > body_rect.bottom(), (
            f"button {btn.text()!r} ({btn_rect}) must sit below body ({body_rect})"
        )
        assert btn.width() >= btn.sizeHint().width(), (
            f"button {btn.text()!r} squeezed ({btn.width()} < {btn.sizeHint().width()})"
        )


def _build_scroll_dialog(
    name: str, tmp_path: Path, monkeypatch, *, tall: bool = False
) -> tuple[QDialog, tuple[QPushButton, ...]]:
    cfg = _make_config(tmp_path)
    if name == "add_game":
        dlg = AddGameDialog(cfg, data_root=tmp_path / "profiles")
        if tall:
            dlg._toggle_location()
        return dlg, _find_buttons(dlg, ("Cancel", "Add game"))

    if name == "profile_folders":
        count = 16 if tall else 4
        folders = []
        for i in range(1, count + 1):
            path = tmp_path / "profiles" / f"blue-protocol-star-resonance-copy-{i}"
            path.mkdir(parents=True, exist_ok=True)
            folders.append(profilefolders.Folder(path, i * 12 * 1024**3))
        dlg = ProfileFoldersDialog(cfg, folders)
        return dlg, (dlg.delete_button, *_find_buttons(dlg, ("Close",)))

    if name == "runtime_folders":
        count = 14 if tall else 2
        items: list[tuple[Path, int]] = []
        for i in range(count):
            path = tmp_path / "runtimes" / f"umu-runtime-{i}"
            path.mkdir(parents=True, exist_ok=True)
            items.append((path, (400 + i * 50) * 1024**2))
        dlg = RuntimeFoldersDialog(cfg, items)
        return dlg, (dlg.delete_button, *_find_buttons(dlg, ("Close",)))

    if name == "found_copies":
        copies = [
            gamefinder.Copy(
                game_dir=tmp_path
                / "drives"
                / f"library-{i}"
                / "Nadare"
                / "Blue Protocol Star Resonance",
                library=tmp_path / "drives" / f"library-{i}" / "Nadare",
                install_dir=tmp_path
                / "drives"
                / f"library-{i}"
                / "Nadare"
                / "Blue Protocol Star Resonance"
                / "install",
                prefix=tmp_path
                / "drives"
                / f"library-{i}"
                / "Nadare"
                / "Blue Protocol Star Resonance"
                / "prefix",
                made_by_nadare=("install", "prefix"),
                version=f"1.{i}.0",
                complete=(i == 1),
                recorded=(i == 1),
                from_manifest=True,
                kind="direct-bpsr",
            )
            for i in range(1, 5)
        ]
        dlg = FoundCopiesDialog("Blue Protocol: Star Resonance", copies)
        return dlg, _find_buttons(dlg, ("Cancel", "Not now", "Use this folder"))

    if name == "spare_copies":
        count = 8 if tall else 2
        spares = [
            gamefinder.Spare(
                game_dir=tmp_path
                / "drives"
                / f"old-copy-{i}"
                / "Nadare"
                / "Blue Protocol Star Resonance",
                key=key,
                path=tmp_path
                / "drives"
                / f"old-copy-{i}"
                / "Nadare"
                / "Blue Protocol Star Resonance"
                / key,
                size=(42 if key == "install_dir" else 2) * 1024**3,
            )
            for i in range(1, count + 1)
            for key in ("install_dir", "prefix")
        ]
        dlg = SpareCopiesDialog("Blue Protocol: Star Resonance", spares)
        return dlg, (dlg.delete_button, *_find_buttons(dlg, ("Keep all",)))

    if name == "library_picker":
        dlg = LibraryPicker(cfg, "Move Blue Protocol: Star Resonance")
        box = dlg.findChild(QDialogButtonBox)
        assert box is not None
        return dlg, tuple(box.buttons())

    if name == "safety_result":
        match_count = 24 if tall else 2
        matches = tuple(
            safetycheck.Match(
                id=f"suspicious-indicator-marker-2026-11-{i:02d}",
                count=i + 1,
                level=safetycheck.COMPROMISED,
            )
            for i in range(match_count)
        )
        result = safetycheck.SafetyResult(
            level=safetycheck.COMPROMISED,
            matches=matches,
            checked_at="2026-11-20T14:30:00+00:00",
            files_visited=14280,
        )
        dlg = SafetyResultDialog(None, result, "Blue Protocol: Star Resonance")
        return dlg, _find_buttons(dlg, ("Close",))

    if name == "steam_setup":
        dlg = SteamSetupDialog(
            None,
            plan_fn=lambda: _make_plan(extra_steps=6 if tall else 0),
            act_fn=lambda _item: None,
            async_plan=False,
        )
        return dlg, (dlg.next_button, dlg.check_button, dlg.close_button)

    if name == "userns":
        monkeypatch.setattr(userns, "can_fix", lambda: True)
        msg = userns.with_hint(userns.BLOCKED_BEFORE_LAUNCH, blocked=True)
        dlg = UsernsDialog(msg, retry_op="launch")
        if tall:
            dlg._toggle_details()
            dlg.status.setText(
                "Could not load the AppArmor profile.\n"
                + "\n".join(
                    f"apparmor_parser: detail line {i} explaining the permission issue in full"
                    for i in range(1, 9)
                )
            )
            dlg._fit_height()
        return dlg, (
            dlg.details_button,
            dlg.copy_button,
            dlg.fix_button,
            dlg.close_button,
        )

    raise ValueError(name)


SCROLL_DIALOGS = (
    "add_game",
    "profile_folders",
    "runtime_folders",
    "found_copies",
    "spare_copies",
    "library_picker",
    "safety_result",
    "steam_setup",
    "userns",
)

TALL_SCROLL_DIALOGS = (
    "profile_folders",
    "runtime_folders",
    "spare_copies",
    "safety_result",
    "steam_setup",
    "userns",
)

RESIZABLE_DIALOGS = (
    "logs",
    "manage_runners",
    "support_report",
    "system_check",
    "terms",
)


def _build_resizable_dialog(
    name: str, tmp_path: Path, monkeypatch
) -> tuple[QDialog, tuple[QPushButton, ...], QWidget]:
    if name == "logs":
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "nadare.log").write_text("app started\n", encoding="utf-8")
        (logs_dir / "bpsr-launch.log").write_text(
            "===== 2026-11-25T10:00:00 launch (bpsr) | nadare 0.1.0b29 =====\n"
            "wine: starting game\n",
            encoding="utf-8",
        )
        dlg = LogsDialog(["bpsr"], profile="bpsr", directory=logs_dir, follow=False)
        buttons = (
            dlg.copy,
            dlg.save,
            dlg.zip_btn,
            dlg.folder,
            dlg.report_btn,
            dlg.refresh_button,
        )
        return dlg, buttons, dlg.view

    if name == "manage_runners":
        runners_dir = tmp_path / "runners"
        runners_dir.mkdir(parents=True, exist_ok=True)
        for rname in ("dwproton-11.0-17-x86_64", "dwproton-11.0-13-x86_64"):
            rdir = runners_dir / rname
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(runners.paths, "runners_dir", lambda: runners_dir)
        monkeypatch.setattr(runners, "steam_runners", lambda: [])
        dlg = ManageRunnersDialog({"bpsr": {"backend": "direct-bpsr"}})
        return dlg, (dlg.cleanup_button, *_find_buttons(dlg, ("Close",))), dlg.table

    if name == "support_report":
        dlg = ReportDialog(None, "bpsr")
        dlg.set_report("Nadare support report\nDistro: Fedora\nGPU: Radeon RX 7800 XT\n")
        return dlg, (dlg.copy_button, dlg.save_button, *_find_buttons(dlg, ("Close",))), dlg.text

    if name == "system_check":
        dlg = SystemCheckDialog(None, {"profiles": {}})
        dlg._show_result("verdict: ready\nGPU: AMD Radeon RX 7800 XT\nVulkan: 1.4\n")
        return dlg, (dlg.copy_button, dlg.again_button, *_find_buttons(dlg, ("Close",))), dlg.text

    if name == "terms":
        dlg = TermsDialog(
            "Chaos Zero Nightmare",
            lambda: {"pending": True, "documents": []},
            lambda: None,
            "https://policy.example/terms",
        )
        dlg.text.setHtml("<h3>Terms of Service</h3><p>Sample terms text.</p>")
        return dlg, (dlg.browser_button, dlg.cancel_button, dlg.agree_button), dlg.text

    raise ValueError(name)


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("screen_size", SCREENS)
@pytest.mark.parametrize("name", SCROLL_DIALOGS)
def test_scroll_dialogs_use_shared_layout(
    qapp, text_size_env, monkeypatch, tmp_path, name, screen_size, px
):
    text_size_env(px)
    w, h = screen_size
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, w, h))
    dlg, buttons = _build_scroll_dialog(name, tmp_path, monkeypatch)
    try:
        dlg.show()
        qapp.processEvents()
        _assert_shared_layout(dlg, buttons, screen_size=screen_size, scroll_body=True)
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("screen_size", SCREENS)
def test_found_games_choose_box_uses_shared_layout(
    qapp, text_size_env, monkeypatch, screen_size, px
):
    text_size_env(px)
    w, h = screen_size
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, w, h))
    items = [
        (f"Blue Protocol: Star Resonance copy {i}  (/mnt/games/Nadare/BPSR-{i})", i % 2 == 0)
        for i in range(1, 15)
    ]
    checked: list[bool] = []

    def inspect_exec(dialog: QDialog) -> int:
        dialog.show()
        qapp.processEvents()
        box = dialog.findChild(QDialogButtonBox)
        assert box is not None
        buttons = tuple(box.buttons())
        _assert_shared_layout(dialog, buttons, screen_size=screen_size, scroll_body=True)
        if screen_size == (1024, 600) and px == 20:
            area = dialog.findChild(QScrollArea)
            assert area is not None and area.verticalScrollBar().maximum() > 0
        checked.append(True)
        dialog.hide()
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", inspect_exec)
    panel = LibrariesPanel()
    try:
        assert panel._choose_box(items) is None
        assert checked == [True]
    finally:
        panel.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("name", TALL_SCROLL_DIALOGS)
def test_tall_scroll_dialog_scrolls_body_and_fits_1024x600(
    qapp, text_size_env, monkeypatch, tmp_path, name
):
    text_size_env(20)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1024, 600))
    dlg, buttons = _build_scroll_dialog(name, tmp_path, monkeypatch, tall=True)
    try:
        dlg.show()
        qapp.processEvents()
        assert dlg.height() <= 600
        area = dlg.findChild(QScrollArea)
        assert area is not None
        assert area.verticalScrollBar().maximum() > 0
        _assert_shared_layout(dlg, buttons, screen_size=(1024, 600), scroll_body=True)
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("name", SCROLL_DIALOGS)
def test_show_event_refits_dialog_after_screen_shrinks(
    qapp, text_size_env, monkeypatch, tmp_path, name
):
    """Removing fit_dialog from showEvent must fail when the screen shrinks before show."""
    text_size_env(20)
    avail = [QRect(0, 0, 1920, 1080)]
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: avail[0])
    dlg, buttons = _build_scroll_dialog(
        name, tmp_path, monkeypatch, tall=name in TALL_SCROLL_DIALOGS
    )
    try:
        # Force a wrong height (or oversized from 1920x1080) before first show.
        dlg.resize(dlg.width(), 100)
        avail[0] = QRect(0, 0, 1024, 600)
        dlg.show()
        qapp.processEvents()
        assert dlg.height() > 120, f"{name}: showEvent did not refit height"
        _assert_shared_layout(dlg, buttons, screen_size=(1024, 600), scroll_body=True)
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("screen_size", SCREENS)
@pytest.mark.parametrize("name", RESIZABLE_DIALOGS)
def test_resizable_dialogs_have_shared_chrome_and_stay_resizable(
    qapp, text_size_env, monkeypatch, tmp_path, name, screen_size, px
):
    text_size_env(px)
    w, h = screen_size
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, w, h))
    dlg, buttons, body = _build_resizable_dialog(name, tmp_path, monkeypatch)
    try:
        dlg.show()
        qapp.processEvents()
        _assert_shared_layout(
            dlg, buttons, screen_size=screen_size, scroll_body=False, body_widget=body
        )
        # Must stay resizable (not locked to a fixed size).
        assert dlg.minimumHeight() < dlg.maximumHeight()
        assert dlg.minimumWidth() < dlg.maximumWidth()
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("kind", ["account_notice", "hold"])
def test_notice_and_hold_keep_user_width_on_reshow(qapp, text_size_env, monkeypatch, kind):
    """#1809 review item 3: only fit width on first show; later shows keep user width."""
    text_size_env(13)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    notice = str(getattr(core_backends.get("direct-dna"), "account_notice", "") or "")
    assert notice
    if kind == "account_notice":
        dlg = AccountNoticeDialog(notice)
        buttons = (dlg.continue_button, dlg.cancel_button)
    else:
        hold = SimpleNamespace(reason=notice, link="https://example.invalid/hold")
        dlg = HoldDialog(hold)
        buttons = (dlg.start_button, dlg.cancel_button)

    try:
        dlg.show()
        qapp.processEvents()
        initial_w = dlg.width()
        wider_w = initial_w + 240
        dlg.resize(wider_w, dlg.height())
        qapp.processEvents()
        assert dlg.width() == wider_w

        dlg.hide()
        qapp.processEvents()
        dlg.show()
        qapp.processEvents()
        assert dlg.width() == wider_w, (
            f"{kind} reset width from {wider_w} back to {dlg.width()} on reshow"
        )
        _assert_dialog_layout_clean(
            dlg, buttons, shown=True, max_size=(1280, 720), screen_height=720
        )
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


def _assert_checkbox_rows_readable(
    dialog: QDialog,
    boxes: list,
    expected_substrings: list[tuple[str, ...]],
) -> None:
    """Every checkbox row's full text fits its visible width without right-edge clipping."""
    area = dialog.findChild(QScrollArea)
    assert area is not None
    vp = area.viewport()
    assert len(boxes) == len(expected_substrings) and boxes

    for box, required in zip(boxes, expected_substrings, strict=True):
        full_text = box.text()
        for part in required:
            assert part in full_text, f"missing {part!r} in row text {full_text!r}"
        tip = box.toolTip()
        assert tip, f"row tooltip must not be empty for {full_text!r}"

        if isinstance(box, QCheckBox):
            # Plain QCheckBox does not wrap: it clips whenever narrower than its sizeHint.
            assert box.width() >= box.sizeHint().width(), (
                f"checkbox text clipped horizontally ({box.width()} < {box.sizeHint().width()}): "
                f"{full_text!r}"
            )
            continue

        # Composite wrapped checkbox: check the buddy label is word-wrapped, unclipped,
        # fits inside the scroll viewport, and toggles the checkbox on click.
        label = box.findChild(QLabel)
        check = box.findChild(QCheckBox)
        assert label is not None and check is not None
        assert check.text() == "", "inner QCheckBox should carry no duplicate text"
        assert label.wordWrap(), f"row label must be word-wrapped: {full_text!r}"
        assert label.text() == full_text
        assert label.width() > 0 and label.height() > 0
        assert label.minimumSizeHint().width() <= label.width(), (
            f"row label narrower than its widest token ({label.width()} < "
            f"{label.minimumSizeHint().width()}): {full_text!r}"
        )
        need_h = label.heightForWidth(label.width())
        assert need_h <= label.height(), (
            f"row label clipped vertically ({label.height()} < {need_h}): {full_text!r}"
        )
        label_rect = _rect_in(label, vp)
        assert label_rect.left() >= 0 and label_rect.right() <= vp.rect().right(), (
            f"row label {label_rect} overflows viewport {vp.rect()}: {full_text!r}"
        )

    # Clicking the first row's buddy label toggles its checkbox state.
    first = boxes[0]
    first_label = first.findChild(QLabel)
    if first_label is not None:
        was = first.isChecked()
        center = QPointF(first_label.rect().center())
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            center,
            center,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            center,
            center,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        first_label.mousePressEvent(press)
        first_label.mouseReleaseEvent(release)
        assert first.isChecked() is (not was), "clicking row label must toggle checkbox"
        first.setChecked(was)


@pytest.mark.parametrize("px", TEXT_SIZES)
@pytest.mark.parametrize("screen_size", SCREENS)
@pytest.mark.parametrize(
    "name",
    ("profile_folders", "runtime_folders", "spare_copies", "found_games"),
)
def test_checkbox_rows_not_clipped_and_keep_full_text(
    qapp, text_size_env, monkeypatch, tmp_path, name, screen_size, px
):
    """Fix round 1 item 1: checkbox rows wrap cleanly and never clip size/path at 20 px."""
    text_size_env(px)
    w, h = screen_size
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, w, h))

    if name == "found_games":
        items = [
            (
                f"Blue Protocol: Star Resonance copy {i}  "
                f"(/mnt/games/Nadare/Blue Protocol Star Resonance-{i})",
                i % 2 == 0,
            )
            for i in range(1, 6)
        ]
        checked: list[bool] = []

        def inspect_exec(dialog: QDialog) -> int:
            dialog.show()
            qapp.processEvents()
            expected = [(f"copy {i}", f"Star Resonance-{i})") for i in range(1, 6)]
            _assert_checkbox_rows_readable(dialog, dialog.boxes, expected)
            for box, (full_label, _tick) in zip(dialog.boxes, items, strict=True):
                assert full_label in box.toolTip()
            checked.append(True)
            dialog.hide()
            return int(QDialog.DialogCode.Rejected)

        monkeypatch.setattr(QDialog, "exec", inspect_exec)
        panel = LibrariesPanel()
        try:
            assert panel._choose_box(items) is None
            assert checked == [True]
        finally:
            panel.deleteLater()
            qapp.processEvents()
        return

    if name == "profile_folders":
        cfg = _make_config(tmp_path)
        names = (
            "blue-protocol-star-resonance-archived-backup-profile-from-older-ssd-drive-copy-1",
            "blue-protocol-star-resonance-copy-2",
            "blue-protocol-star-resonance-copy-3",
        )
        folders = []
        for idx, folder_name in enumerate(names, start=1):
            path = tmp_path / "profiles" / folder_name
            path.mkdir(parents=True, exist_ok=True)
            folders.append(profilefolders.Folder(path, idx * 12 * 1024**3))
        dlg = ProfileFoldersDialog(cfg, folders)
        buttons = (dlg.delete_button, *_find_buttons(dlg, ("Close",)))
    else:
        dlg, buttons = _build_scroll_dialog(name, tmp_path, monkeypatch)
    try:
        dlg.show()
        qapp.processEvents()
        _assert_shared_layout(dlg, buttons, screen_size=screen_size, scroll_body=True)
        if name == "profile_folders":
            expected = [(f"copy-{i}", f"{i * 12} GiB") for i in range(1, 4)]
            paths = [str(f.path) for f in dlg._folders]
        elif name == "runtime_folders":
            expected = [("Shared umu runtime", "MiB") for _ in range(len(dlg.boxes))]
            paths = [str(f.path) for f in dlg._folders]
        else:
            expected = [
                ("42 GiB" if s.key == "install_dir" else "2 GiB", str(s.path))
                for s in dlg._spares
            ]
            paths = [str(s.path) for s in dlg._spares]
        _assert_checkbox_rows_readable(dlg, dlg.boxes, expected)
        for box, full_path in zip(dlg.boxes, paths, strict=True):
            assert full_path in box.toolTip()
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()


def test_library_picker_tooltips_include_full_path_and_free_space(
    qapp, text_size_env, tmp_path, monkeypatch
):
    """Fix round 1 item 2: every LibraryPicker item tooltip has full path and free space."""
    text_size_env(20)
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1280, 720))
    cfg = _make_config(tmp_path)
    missing = tmp_path / "drives" / "unplugged-usb-drive" / "Nadare"
    cfg.data[libraries.CONFIG_KEY].append({"path": str(missing)})
    cfg.save()

    dlg = LibraryPicker(cfg, "Move Blue Protocol: Star Resonance")
    try:
        dlg.show()
        qapp.processEvents()
        assert dlg.list.count() >= 4
        checked_libraries = 0
        for index in range(dlg.list.count()):
            item = dlg.list.item(index)
            assert item is not None
            root = item.data(Qt.ItemDataRole.UserRole)
            tip = item.toolTip()
            if root is None:
                # "Another folder…" row
                assert tip
                continue
            checked_libraries += 1
            root_path = Path(root)
            assert str(root_path) in tip, f"tooltip missing full path {root_path}: {tip!r}"
            if root_path == missing:
                assert "unplugged" in tip or "not found" in tip, (
                    f"unplugged library tooltip missing status: {tip!r}"
                )
            else:
                assert "free" in tip, f"tooltip missing free space for {root_path}: {tip!r}"
        assert checked_libraries >= 3
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.processEvents()
