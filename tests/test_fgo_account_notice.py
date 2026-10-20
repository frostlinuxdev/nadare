# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run account-notice gate for the FGO lepton backend.

Covers the backend contract helper, the FGO install/launch refusal, the GUI
modal flow and the CLI opt-in.  Nothing here touches the network or a real
lepton-fgo install.
"""

from __future__ import annotations

import os

import pytest
from _backend_helpers import make_context

from nadare.backends.fgo import installer, lepton
from nadare.backends.fgo.backend import ACCOUNT_NOTICE, FgoLeptonBackend
from nadare.core.backends import BackendContext, BackendError, GameBackend, Status
from nadare.core.config import Config

# ---------------------------------------------------------------------------
# backend contract
# ---------------------------------------------------------------------------


class _NoticeBackend(GameBackend):
    kind = "notice-test"
    account_notice = "a notice"

    def status(self) -> Status:
        return Status("installed")

    def install(self) -> None:
        pass

    def update(self) -> None:
        pass

    def launch(self) -> int:
        return 0

    def stop(self) -> None:
        pass


def test_requires_notice_ack_defaults_and_flag(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="notice-test")
    backend = _NoticeBackend(BackendContext("p", cfg.profile("p"), cfg))

    assert backend.requires_notice_ack() is True
    assert backend.requires_notice_ack({"accept_account_notice": True}) is False
    assert backend.requires_notice_ack({"accept_account_notice": False}) is True

    cfg.profile("p")["accept_account_notice"] = True
    assert backend.requires_notice_ack() is False


def test_backend_without_notice_never_requires_ack(tmp_path):
    class _Plain(_NoticeBackend):
        account_notice = None

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="notice-test")
    backend = _Plain(BackendContext("p", cfg.profile("p"), cfg))
    assert backend.requires_notice_ack() is False


# ---------------------------------------------------------------------------
# FGO install/launch gate
# ---------------------------------------------------------------------------


def test_install_raises_without_ack(tmp_path):
    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    with pytest.raises(BackendError) as excinfo:
        backend.install()
    assert excinfo.value.code == 2
    assert "account notice" in excinfo.value.message.lower()


def test_launch_raises_without_ack(tmp_path):
    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    with pytest.raises(BackendError) as excinfo:
        backend.launch()
    assert excinfo.value.code == 2
    assert "account notice" in excinfo.value.message.lower()


def test_install_with_ack_passes_accept_flag(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    seen: dict = {}

    class FakeAdapter:
        context = "fgo"

        def check_host(self):
            return {"ok": True, "output": ""}

        def selinux_root_command(self):
            return ""

        def cmd_install(self, **kwargs):
            seen.update(kwargs)
            return ["install.sh"]

        def set_repo(self, repo):
            pass

    backend.adapter = FakeAdapter()  # type: ignore[assignment]
    monkeypatch.setattr(backend, "_stream", lambda cmd: 0)
    monkeypatch.setattr(installer, "ensure_installer", lambda adapter, **k: tmp_path)

    backend.install()

    assert seen["accept_notice"] is True


def test_install_without_ack_never_reaches_lepton(tmp_path, monkeypatch):
    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    reached: list = []
    monkeypatch.setattr(backend.adapter, "check_host", lambda: reached.append("host"))
    with pytest.raises(BackendError):
        backend.install()
    assert reached == []


def test_launch_with_ack_proceeds(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    exe = tmp_path / "lepton-fgo"
    exe.write_text("#!/bin/sh\n")

    class FakeAdapter:
        lepton_game = tmp_path / "missing-game"
        lepton_fgo = exe

        def cmd_launch(self):
            return ["launch"]

    backend.adapter = FakeAdapter()  # type: ignore[assignment]
    monkeypatch.setattr(backend, "_stream", lambda cmd: 0)

    assert backend.launch() == 0


def test_cmd_install_default_does_not_accept_notice(tmp_path):
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    assert "--accept-account-notice" not in adapter.cmd_install()
    assert "--accept-account-notice" in adapter.cmd_install(accept_notice=True)


def test_notice_text_discloses_no_app_data_details():
    lowered = ACCOUNT_NOTICE.lower()
    for forbidden in ("file", "copy", "folder", "data/"):
        assert forbidden not in lowered


def test_notice_text_is_the_exact_ack():
    assert ACCOUNT_NOTICE.startswith("BEFORE YOU PLAY: SECURE YOUR ACCOUNT")
    assert "Aniplex" in ACCOUNT_NOTICE
    assert "Issue Transfer Number" in ACCOUNT_NOTICE


# ---------------------------------------------------------------------------
# GUI gate
# ---------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QDialog
except Exception:  # noqa: BLE001
    QDialog = None  # type: ignore[assignment]

pytestmark_gui = pytest.mark.skipif(QDialog is None, reason="PyQt6 is not importable")


@pytest.fixture(name="qt_env")
def _qt_env():
    if QDialog is None:
        pytest.skip("PyQt6 is not importable")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytestmark_gui
def test_notice_dialog_continue_disabled_until_ticked(qt_env):
    from nadare.gui.notice import NOTICE_ACK_TEXT, AccountNoticeDialog

    dialog = AccountNoticeDialog(ACCOUNT_NOTICE)
    try:
        assert ACCOUNT_NOTICE in dialog.notice_label.text()
        assert dialog.checkbox.text() == NOTICE_ACK_TEXT
        assert not dialog.continue_button.isEnabled()
        dialog.checkbox.setChecked(True)
        assert dialog.continue_button.isEnabled()
        dialog.checkbox.setChecked(False)
        assert not dialog.continue_button.isEnabled()
    finally:
        dialog.deleteLater()
        qt_env.processEvents()


def _make_window(tmp_path, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("gfgo", backend="fgo-lepton", title="FGO")
    window = mw.MainWindow(config, profile="gfgo")
    starts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window.controller, "start", lambda p, op, payload=None: starts.append((p, op)) or True
    )
    return mw, window, config, starts


@pytestmark_gui
def test_gui_continue_persists_flag_and_starts_install(tmp_path, qt_env, monkeypatch):
    mw, window, config, starts = _make_window(tmp_path, monkeypatch)
    shown: list[str] = []

    class FakeDialog:
        def __init__(self, notice, parent=None):
            shown.append(notice)

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "AccountNoticeDialog", FakeDialog)
    try:
        window._on_game_action("install")
        assert config.profile("gfgo")["accept_account_notice"] is True
        assert starts == [("gfgo", "install")]
        assert Config.load(config.path).profile("gfgo")["accept_account_notice"] is True

        # Second attempt must not prompt again.
        window._on_game_action("install")
        assert len(shown) == 1
        assert starts == [("gfgo", "install"), ("gfgo", "install")]
    finally:
        window.close()
        window.deleteLater()
        qt_env.processEvents()


@pytestmark_gui
def test_gui_cancel_starts_nothing(tmp_path, qt_env, monkeypatch):
    mw, window, config, starts = _make_window(tmp_path, monkeypatch)

    class FakeDialog:
        def __init__(self, notice, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw, "AccountNoticeDialog", FakeDialog)
    try:
        window._on_game_action("install")
        assert config.profile("gfgo").get("accept_account_notice", False) is False
        assert starts == []
    finally:
        window.close()
        window.deleteLater()
        qt_env.processEvents()


@pytestmark_gui
def test_gui_non_gated_operation_needs_no_notice(tmp_path, qt_env, monkeypatch):
    mw, window, config, starts = _make_window(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mw,
        "AccountNoticeDialog",
        lambda *a, **k: pytest.fail("dialog must not be shown for status"),
    )
    try:
        window._on_game_action("status")
        assert starts == [("gfgo", "status")]
    finally:
        window.close()
        window.deleteLater()
        qt_env.processEvents()


# ---------------------------------------------------------------------------
# CLI gate
# ---------------------------------------------------------------------------


def _cli_config(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("gfgo", backend="fgo-lepton", title="FGO")
    config.save()
    return config


def test_cli_unacked_prints_notice_and_exits_2(tmp_path, capsys):
    from nadare.app import run_cli

    config = _cli_config(tmp_path)
    code = run_cli(["-c", str(config.path), "install", "-p", "gfgo"])
    captured = capsys.readouterr()
    assert code == 2
    assert "BEFORE YOU PLAY: SECURE YOUR ACCOUNT" in captured.out
    assert "--accept-account-notice" in captured.err


def test_cli_accept_flag_sets_profile(tmp_path, monkeypatch, capsys):
    from nadare.app import run_cli
    from nadare.backends.fgo import backend as fgo_backend

    calls: list[str] = []
    monkeypatch.setattr(
        fgo_backend.FgoLeptonBackend, "install", lambda self: calls.append(self.ctx.name)
    )

    config = _cli_config(tmp_path)
    code = run_cli(["-c", str(config.path), "install", "-p", "gfgo", "--accept-account-notice"])
    capsys.readouterr()
    assert code == 0
    assert calls == ["gfgo"]
    assert Config.load(config.path).profile("gfgo")["accept_account_notice"] is True
