# SPDX-License-Identifier: GPL-3.0-or-later
"""#990: Wuthering Waves asks for its resource quality in the install confirm, with sizes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import backends as core_backends
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.quick_install import QuickInstallDialog

KIND = "direct-wuwa"


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_wuwa_declares_its_quality_as_the_install_choice():
    option, sizes = core_backends.install_choice(KIND)
    assert option.key == "wuwa_quality" and option.default == "hd"
    assert set(sizes) == {"sd", "hd", "uhd"} and sizes["sd"] < sizes["hd"] < sizes["uhd"]
    assert core_backends.install_choice("stellasora-yostar") is None


def test_the_dialog_offers_the_choice_with_sizes_and_follows_it(qapp):
    option, sizes = core_backends.install_choice(KIND)
    picked: list[str] = []
    size_now = {"value": sizes["hd"]}

    def describe(chosen):
        return "/games/wuwa", "500 GB free", f"downloads about {size_now['value'] // 2**30} GiB"

    def on_choice(value):
        picked.append(value)
        size_now["value"] = sizes[value]

    dialog = QuickInstallDialog(
        "Wuthering Waves", notice="", describe=describe,
        choice=option, choice_sizes=sizes, on_choice=on_choice,
    )
    try:
        combo = dialog.choice_combo
        labels = [combo.itemText(i) for i in range(combo.count())]
        assert [combo.itemData(i) for i in range(combo.count())] == ["sd", "hd", "uhd"]
        assert all("GiB" in label for label in labels)
        assert dialog.choice_value == "hd"
        combo.setCurrentIndex(0)
        assert picked[-1] == "sd" and dialog.choice_value == "sd"
        assert "about 58 GiB" in dialog.summary_label.text()
    finally:
        dialog.deleteLater()


def test_store_install_stores_the_picked_quality_before_the_install_starts(
    tmp_path, qapp, monkeypatch
):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    cfg = Config.load(tmp_path / "config.json")
    cfg.data["unofficial_notice_ack"] = [KIND]
    window = MainWindow(cfg)
    started: list[tuple[str, str]] = []

    def start(profile, op, *args, **kwargs):
        started.append((profile, op, cfg.profiles[profile].get("wuwa_quality")))
        return True

    monkeypatch.setattr(window, "_start", start)

    def fake_exec(self):
        self.choice_combo.setCurrentIndex(2)  # UHD
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QuickInstallDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(window, "_confirm_notice_text", lambda text: True)
    try:
        window._store_install(KIND)
        assert [s for s in started if s[1] == "install"][-1][2] == "uhd"
        name = next(iter(cfg.profiles))
        assert Config.load(tmp_path / "config.json").profiles[name]["wuwa_quality"] == "uhd"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
