# SPDX-License-Identifier: GPL-3.0-or-later
"""b20 GUI/text follow-ups: small screens, text scaling, wording and FGO import."""

from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QRect, QSize
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import theme
from nadare.gui.add_game import PREFIX_ONLY_KINDS, AddGameDialog, detect_import


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---- #326 fit a small/scaled screen -------------------------------------------
def test_window_minimum_never_exceeds_the_screen():
    from nadare.gui import main_window

    smaller = main_window._window_minimum(QSize(880, 560), QRect(0, 0, 1024, 512))
    assert smaller.width() <= 1024
    assert smaller.height() <= 512


def test_first_run_size_fits_the_available_geometry():
    from nadare.gui import main_window

    size = main_window._window_size(QSize(880, 560), QRect(0, 0, 911, 512))
    assert size.width() <= 911
    assert size.height() <= 512
    assert size.height() >= 400


# ---- #330 follow the desktop text size ----------------------------------------
def _base_font_px(css: str) -> int:
    block = css.split("* {", 1)[1].split("}", 1)[0]
    match = re.search(r"font-size:\s*(\d+)px", block)
    assert match is not None
    return int(match.group(1))


def test_stylesheet_scales_with_the_application_font(qapp):
    original = QFont(qapp.font())
    # Leave the app's font watcher connected: conftest sweeps QMessageBoxes that tests build
    # but never show (the stale-box crash), so this covers the real refresh path.
    try:
        small = QFont(qapp.font())
        small.setPointSizeF(9)
        qapp.setFont(small)
        small_px = _base_font_px(theme.stylesheet())

        large = QFont(qapp.font())
        large.setPointSizeF(20)
        qapp.setFont(large)
        large_px = _base_font_px(theme.stylesheet())
    finally:
        qapp.setFont(original)
    assert large_px >= small_px * 2
    assert small_px <= 13


def test_apply_theme_refreshes_the_stylesheet_on_a_font_change(qapp):
    original = QFont(qapp.font())
    try:
        theme.apply_theme(qapp)
        large = QFont(qapp.font())
        large.setPointSizeF(20)
        qapp.setFont(large)
        qapp.processEvents()
        assert _base_font_px(qapp.styleSheet()) >= 20
    finally:
        qapp.setFont(original)
        qapp.setStyleSheet("")


# ---- #342 F-08 dim text contrast ----------------------------------------------
def test_dim_text_meets_wcag_aa_contrast_on_the_canvas():
    def luminance(colour: str) -> float:
        values = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(theme.DIM), luminance(theme.BG)), reverse=True)
    assert (lighter + 0.05) / (darker + 0.05) >= 4.5


# ---- #351 F-10 / F-14 wording -------------------------------------------------
def test_qr_hint_says_where_to_get_stove():
    from nadare.gui.game_view import _QR_SCAN_HINT

    assert "onstove.com" in _QR_SCAN_HINT


def test_runners_empty_state_says_how_a_runner_arrives():
    from nadare.gui import runners_dialog

    assert "install" in runners_dialog.EMPTY_RUNNERS_TEXT.lower()
    assert "Nadare" in runners_dialog.EMPTY_RUNNERS_TEXT


# ---- #350 `remove` help matches what the flags delete -------------------------
def test_remove_help_describes_every_delete_flag():
    from nadare.app import build_parser

    parser = build_parser()
    sub = next(
        action for action in parser._actions if action.dest == "command" and action.choices
    )
    remove = sub.choices["remove"]
    flags = {
        option: action.help
        for action in remove._actions
        if action.dest in ("delete_data", "delete_install_dir", "delete_prefix")
        for option in action.option_strings
        if option.startswith("--")
    }
    assert "files are kept unless" in parser.format_help()
    assert "--delete-data" in flags and "profile" in flags["--delete-data"].lower()
    assert "--delete-install-dir" in flags and "game" in flags["--delete-install-dir"].lower()
    assert "--delete-prefix" in flags and "prefix" in flags["--delete-prefix"].lower()
    joined = " ".join(flags.values()).lower()
    assert "actual game files" in joined or "game files" in joined


# ---- #332 F-09 the verify prompt defaults to No ------------------------------
def test_check_files_prompt_defaults_to_no(monkeypatch):
    from nadare.gui import main_window

    calls: list[tuple] = []

    def question(*args, **kwargs):
        calls.append((args, kwargs))
        return main_window.QMessageBox.StandardButton.No

    monkeypatch.setattr(main_window.QMessageBox, "question", question)
    assert main_window._ask_check_files(None, "Game") is False
    assert calls
    args, kwargs = calls[0]
    default = kwargs.get("defaultButton", args[4])
    assert default == main_window.QMessageBox.StandardButton.No


# ---- #338 F-07 login before install ------------------------------------------
def test_login_is_offered_before_the_game_is_installed(qapp):
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    try:
        view.set_account_controls(True)
        view.set_status(Status("not_installed", data={"logged_in": False}))
        assert view.login.isEnabled()
    finally:
        view.deleteLater()


# ---- #338 F-10 STOVE nickname in status() ------------------------------------
@pytest.fixture
def make_backend_context(tmp_path):
    from _backend_helpers import make_context

    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


def test_czn_status_names_the_logged_in_account(make_backend_context):
    import json

    from nadare.backends.czn.backend import CznBackend

    backend = CznBackend(make_backend_context("stove-czn", {}))
    session = backend._session()
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        json.dumps(
            {
                "access_token": "tok",
                "refresh_token": "refresh",
                "member_nickname": "Cat",
                "expire_time": "9999999999999",
            }
        )
    )
    status = backend.status()
    assert status.data["account"] == "Cat"


# ---- #338 F-11 remember-me wording -------------------------------------------
def test_remember_me_help_avoids_keyring_jargon():
    from nadare.gui import game_view, settings

    for text in (game_view._REMEMBER_TIP, settings.REMEMBER_HINT):
        lowered = text.lower()
        assert "keyring" not in lowered
        assert "doctor" not in lowered


# ---- #336 F-07 / F-08 --------------------------------------------------------
def test_fgo_catalog_entry_is_labelled_na():
    from nadare.gui.add_game import available_catalog

    games = {game.kind: game for game in available_catalog()}
    assert "NA" in games["fgo-lepton"].display


def test_missing_gpu_is_not_reported_as_ok():
    from nadare.backends.fgo import readiness

    check = readiness._gpu_check(readiness.Probe(glob=lambda pattern: []))
    assert check.ok is False
    assert check.tier == "warning"


# ---- #336 F-09 the script backend's Wine prefix is editable -------------------
def test_script_backend_offers_an_editable_prefix():
    from nadare.backends.script import ScriptBackend

    assert any(option.key == "prefix" for option in ScriptBackend.options)


# ---- #288 import an existing lepton-fgo prefix --------------------------------
def _lepton_prefix(root: Path) -> Path:
    prefix = root / "lepton-nv"
    (prefix / "lepton-x86_64").mkdir(parents=True)
    (prefix / "lepton-x86_64" / "lepton-fgo").write_text("#!/bin/sh\n")
    return prefix


def test_detect_import_finds_a_lepton_prefix(tmp_path):
    prefix = _lepton_prefix(tmp_path)
    info = detect_import("fgo-lepton", prefix, None)
    assert info["prefix"] == str(prefix)
    assert info["install_dir"] == ""
    assert info["notes"]


def test_detect_import_rejects_a_folder_without_lepton(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_import("fgo-lepton", empty, None)["notes"] == []
    assert "fgo-lepton" in PREFIX_ONLY_KINDS


def test_import_existing_fgo_keeps_the_lepton_prefix(tmp_path, qapp):
    prefix = _lepton_prefix(tmp_path)
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("fgo-lepton")
        dialog.set_mode(True)
        dialog.set_install_dir(prefix)
        assert dialog.create_profile() == "fate-grand-order"
        profile = config.profile("fate-grand-order")
        assert profile["prefix"] == str(prefix)
        assert not profile.get("install_dir")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- #361 F-11 a missing check-host.sh names the real fix ---------------------
def test_missing_check_host_script_says_how_to_restore_it(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.fgo import installer
    from nadare.backends.fgo.backend import FgoLeptonBackend
    from nadare.core.backends import BackendError

    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    monkeypatch.setattr(backend, "requires_notice_ack", lambda: False)
    monkeypatch.setattr(installer, "ensure_installer", lambda *a, **k: tmp_path / "repo")
    monkeypatch.setattr(
        backend.adapter,
        "check_host",
        lambda **k: {"ok": False, "missing": ["check-host.sh not found"]},
    )
    with pytest.raises(BackendError) as exc:
        backend.install()
    message = exc.value.message
    assert "check-host.sh" in message
    assert "lepton-fgo" in message
    assert "package manager" not in message.lower()
    assert "install" in message.lower()


# ---- #204 CZN X11 limitation is documented --------------------------------
def test_czn_x11_limitation_is_documented():
    root = Path(__file__).resolve().parents[1]
    testers = (root / "docs" / "TESTERS.md").read_text(encoding="utf-8")
    lowered = testers.lower()
    assert "xwayland" in lowered
    assert "blank" in lowered or "flat" in lowered


def test_a_stock_ten_point_desktop_keeps_the_designed_sizes(qapp):
    """A default KDE/GNOME desktop is 10 pt: nothing may grow until the text setting does."""
    original = QFont(qapp.font())
    try:
        nine = QFont(qapp.font())
        nine.setPointSizeF(9)
        qapp.setFont(nine)
        designed = theme.stylesheet()
        ten = QFont(qapp.font())
        ten.setPointSizeF(10)
        qapp.setFont(ten)
        assert theme.stylesheet() == designed
        eleven = QFont(qapp.font())
        eleven.setPointSizeF(12)
        qapp.setFont(eleven)
        assert theme.stylesheet() != designed
    finally:
        qapp.setFont(original)
