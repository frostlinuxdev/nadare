# SPDX-License-Identifier: GPL-3.0-or-later
"""#668: a System check window for people who never open a terminal."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare import app as cli_app
from nadare.core import artwork
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.system_check import SystemCheckDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _flat(dialog) -> str:
    """The shown text with the table's cell gaps squeezed to single spaces."""
    return " ".join(dialog.text.toPlainText().split())


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_the_check_runs_off_the_gui_thread_and_shows_the_result(qapp, monkeypatch):
    threads: list[threading.Thread] = []

    def fake_doctor(config):
        threads.append(threading.current_thread())
        return "GPU: ok\nVulkan: ok\n"

    monkeypatch.setattr(cli_app, "_doctor_text", fake_doctor)
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        dialog.run_check()
        assert "Checking" in dialog.text.toPlainText()
        assert not dialog.again_button.isEnabled()
        assert _wait(qapp, lambda: "GPU: ok" in _flat(dialog))
        assert threads and threads[0] is not threading.main_thread()
        assert dialog.again_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_a_check_that_crashes_says_so_instead_of_staying_blank(qapp, monkeypatch):
    def boom(config):
        raise RuntimeError("no vulkaninfo")

    monkeypatch.setattr(cli_app, "_doctor_text", boom)
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        dialog.run_check()
        assert _wait(qapp, lambda: "could not run" in dialog.text.toPlainText().lower())
        assert "no vulkaninfo" in dialog.text.toPlainText()
        assert dialog.again_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_check_again_runs_it_once_more(qapp, monkeypatch):
    runs: list[int] = []
    monkeypatch.setattr(
        cli_app, "_doctor_text", lambda config: runs.append(1) or f"run {len(runs)}"
    )
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        dialog.run_check()
        assert _wait(qapp, lambda: "run 1" in dialog.text.toPlainText())
        dialog.again_button.click()
        assert _wait(qapp, lambda: "run 2" in dialog.text.toPlainText())
    finally:
        dialog.close()
        dialog.deleteLater()


def test_copy_puts_the_result_on_the_clipboard(qapp, monkeypatch):
    monkeypatch.setattr(cli_app, "_doctor_text", lambda config: "result text")
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        dialog.run_check()
        assert _wait(qapp, lambda: "result text" in dialog.text.toPlainText())
        dialog.copy_button.click()
        assert QApplication.clipboard().text() == "result text"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_the_dialog_says_nothing_is_changed_or_uploaded(qapp, monkeypatch):
    monkeypatch.setattr(cli_app, "_doctor_text", lambda config: "")
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        text = dialog.intro.text().lower()
        assert "nothing is changed" in text and "uploaded" in text
    finally:
        dialog.close()
        dialog.deleteLater()


def test_settings_has_a_system_check_button_that_opens_the_window(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(cli_app, "_doctor_text", lambda config: "all fine")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="direct-pgr", title="A")
    config.save()
    window = MainWindow(config)
    try:
        assert window.settings.system_check_button.text() == "System check"
        window.settings.system_check_button.click()
        dialog = window._system_check_dialog
        assert isinstance(dialog, SystemCheckDialog)
        assert _wait(qapp, lambda: "all fine" in dialog.text.toPlainText())
        dialog.close()
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def teardown_module(module):
    QThreadPool.globalInstance().waitForDone(2000)


def test_the_system_check_button_is_not_crammed_into_the_settings_footer(tmp_path, qapp):
    """At the minimum window width the footer already clips; the button lives in the body."""
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="direct-pgr", title="A")
    config.save()
    window = MainWindow(config)
    try:
        settings = window.settings
        button = settings.system_check_button
        assert button.parentWidget() is not settings.report_button.parentWidget()
        assert button.parentWidget() is settings.check_updates.parentWidget()
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_result_text_uses_a_readable_style(qapp):
    from nadare.gui.support_report import ReportDialog

    check = SystemCheckDialog(None, {"profiles": {}})
    report = ReportDialog(None, "a")
    try:
        assert check.text.objectName() == "Report"  # the same dark panel, set out as a list
        assert report.text.objectName() == "Console"
    finally:
        check.deleteLater()
        report.deleteLater()


# ---- readable layout: the same text, set out as a list instead of a terminal dump ----------
SAMPLE = """\
GPU/Vulkan: Vulkan 1.4.305 on AMD Radeon RX 7900 XTX (RADV NAVI31) (radv) [DXVK-ready]
umu-run: not found (Nadare can set up its own copy at launch)
32-bit libs: missing libfreetype.so.6, libudev.so.1
  not needed while Nadare's own runtime works
  or install them: sudo apt install libfreetype6:i386 <b>x</b>
user namespaces: OK
disk space:
  145 GiB free on /
runners: none found
  next: run nadare install -p <profile>
"""


def test_the_report_is_parsed_into_items_notes_and_headings():
    from nadare.gui.system_check import parse_report

    lines = parse_report(SAMPLE)
    kinds = [line.kind for line in lines]
    assert kinds == [
        "item", "item", "item", "note", "note", "item", "heading", "note", "item", "note",
    ]
    gpu = lines[0]
    assert gpu.label == "GPU/Vulkan" and gpu.body.startswith("Vulkan 1.4.305")
    assert lines[6].label == "disk space"


def test_problem_words_flag_a_line_and_plain_lines_are_not_flagged():
    from nadare.gui.system_check import parse_report

    lines = parse_report(SAMPLE)
    flagged = [line.label for line in lines if line.warn]
    assert flagged == ["umu-run", "32-bit libs", "runners"]
    assert not lines[0].warn and not lines[5].warn  # GPU, user namespaces


def test_a_long_sentence_with_a_colon_is_not_taken_for_a_label():
    from nadare.gui.system_check import parse_report

    (line,) = parse_report("The check could not run: no vulkaninfo on this machine at all")
    assert line.kind == "plain" and line.warn


def test_the_html_escapes_everything_and_keeps_the_words(qapp):
    from nadare.gui.system_check import report_html

    html = report_html(SAMPLE)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "&lt;profile&gt;" in html
    assert "3 items flagged" in html


def test_a_clean_report_says_nothing_was_flagged(qapp):
    from nadare.gui.system_check import report_html

    assert "Nothing flagged" in report_html("GPU: ok\nuser namespaces: OK")


def test_the_window_shows_the_readable_layout_but_copies_the_original_text(qapp, monkeypatch):
    monkeypatch.setattr(cli_app, "_doctor_text", lambda config: SAMPLE)
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        dialog.run_check()
        assert _wait(qapp, lambda: "umu-run" in dialog.text.toPlainText())
        shown = dialog.text.toPlainText()
        assert "3 items flagged" in shown  # the summary only exists in the readable view
        dialog.copy_button.click()
        assert QApplication.clipboard().text() == SAMPLE.rstrip("\n") or (
            QApplication.clipboard().text() == SAMPLE
        )
        assert "items flagged" not in QApplication.clipboard().text()
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize(
    "text",
    [
        "GPU/Vulkan: Vulkan software-only [DXVK needs Vulkan >= 1.3]",
        "GPU/Vulkan: Vulkan on a machine with no hardware GPU",
    ],
)
def test_a_gpu_without_hardware_vulkan_is_flagged(qapp, text):
    from nadare.gui.system_check import parse_report

    (line,) = parse_report(text)
    assert line.kind == "item" and line.label == "GPU/Vulkan" and line.warn


def test_a_working_gpu_line_is_not_flagged(qapp):
    from nadare.gui.system_check import parse_report

    (line,) = parse_report("GPU/Vulkan: Vulkan 1.4.305 on AMD Radeon RX 7900 XTX [DXVK-ready]")
    assert not line.warn
