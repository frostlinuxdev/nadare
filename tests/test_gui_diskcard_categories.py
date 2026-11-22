# SPDX-License-Identifier: GPL-3.0-or-later
"""Folded disk categories, batch confirmation, and backup opt-in."""

import time
from datetime import datetime
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QMessageBox

from nadare.core import storagecard as core
from nadare.gui import disk_space_panel
from nadare.gui.disk_space_panel import RECOVERED_TEXT, ClearRow, DiskSpacePanel

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def fake_report(root):
    temps, webs, shaders, backups = [], [], [], []
    for i in range(14):
        size = (0, 22, 3 * MIB, 160 * MIB)[i % 4]
        prefix = root / str(i)
        temps.append(core.PrefixTemp(str(i), f"Game {i}", prefix, (prefix / "Temp",), size, False))
        webs.append(core.WebCache(str(i), f"Game {i}", prefix, (prefix / "Web",), size, False))
        for kind in ("dxvk", "vkd3d"):
            shaders.append(
                core.ShaderCache(
                    f"{kind}:{i}",
                    f"{kind.upper()} · Game {i}",
                    str(i),
                    (prefix / kind,),
                    size,
                    False,
                )
            )
        backups.append(core.Backup(str(i), prefix / f"{i}.bak-pre-fix", prefix, size, False))
    shaders.append(core.ShaderCache("mesa", "Mesa", None, (root / "mesa",), 3 * MIB, False))
    return core.Report(
        temps=tuple(temps),
        webcaches=tuple(webs),
        shaders=tuple(shaders),
        backups=tuple(backups),
        shader_warning="Large shader caches",
    )


@pytest.fixture
def card(qapp, tmp_path, monkeypatch):
    from nadare.core.config import Config

    panel = DiskSpacePanel()
    config = Config.load(tmp_path / "config.json")
    for i in range(14):
        config.add_profile(str(i), backend="wine", title=f"Game {i}")

    def scan(profiles):
        assert len(profiles) == 14
        return fake_report(tmp_path)

    monkeypatch.setattr(core, "scan", scan)
    monkeypatch.setattr(disk_space_panel.runners, "unused_old_runners", lambda _: [])
    monkeypatch.setattr(disk_space_panel.profilefolders, "find", lambda *_: [])
    monkeypatch.setattr(disk_space_panel.umu, "runtime_storage", lambda: [])
    panel.load(config)
    panel.refresh()
    deadline = time.monotonic() + 5
    while panel.report is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert panel.report is not None
    yield panel
    panel.close()
    panel.deleteLater()


def test_four_folded_summaries_and_threshold(card, qapp):
    assert set(card.categories) == {"temp", "web", "shader", "backup"}
    assert len(card.findChildren(ClearRow, options=Qt.FindChildOption.FindDirectChildrenOnly)) == 4
    for key, category in card.categories.items():
        expected = 13 if key == "shader" else 6
        assert len(category.entries) == expected
        assert category.details.isHidden()
        assert category.toggle.isFlat()
        assert all(row.item.size >= MIB for row in category.rows)
        total = (981 if key == "shader" else 489) * MIB
        assert core.diskspace.human_size(total) in category.label.text()
        assert "6 games" in category.label.text()
    card.show()
    card.categories["temp"].toggle.click()
    qapp.processEvents()
    assert card.categories["temp"].details.isVisible()
    assert not card.categories["web"].details.isVisible()
    assert card.shader_warning.text() == "Large shader caches"
    assert "Safe to delete" in card.shader_note.text()


@pytest.mark.parametrize(
    "key,method",
    [
        ("temp", "clear_temp"),
        ("web", "clear_webcache"),
        ("shader", "clear_shader"),
        ("backup", "clear_backup"),
    ],
)
@pytest.mark.parametrize("accept", [False, True])
def test_summary_one_confirmation_and_each_core_call(card, monkeypatch, key, method, accept):
    asked, calls, results = [], [], []

    def question(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)

    def clear(entry):
        calls.append(entry)
        if len(calls) == 1:
            return core.ClearResult(kept=[Path("kept")], reason="permission denied")
        return core.ClearResult(removed=[entry.path if key == "backup" else Path("gone")], freed=7)

    monkeypatch.setattr(core, method, clear)
    monkeypatch.setattr(card, "_start_clear", lambda work: results.append(work()))
    category = card.categories[key]
    category.button.click()
    assert len(asked) == 1
    assert core.diskspace.human_size(category.total) in asked[0]
    for row in category.rows:
        assert row.label.text() in asked[0]
    assert calls == (category.entries if accept else [])
    if accept:
        assert results[0].kept == [Path("kept")]
        assert results[0].reason == "permission denied"
        assert results[0].freed == 7 * (len(calls) - 1)
        monkeypatch.setattr(card, "refresh", lambda: None)
        card._on_cleared(results[0])
        assert "permission denied" in card.status.text()


@pytest.mark.parametrize("tick,accept", [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("summary", [False, True])
def test_backup_title_date_and_confirmation_opt_in(
    qapp, tmp_path, monkeypatch, tick, accept, summary
):
    panel = DiskSpacePanel()
    folder = tmp_path / "5019180.bak-pre-webview2"
    folder.mkdir()
    newest = core.Backup("5019180", folder, tmp_path, 3 * MIB, True)
    older = core.Backup("5019180", tmp_path / "old", tmp_path, 3 * MIB, False)
    panel._fill_report(core.Report(backups=(older, newest)))
    row = panel.backup_rows[1]
    assert "Epic Seven" in row.label.text()
    assert datetime.fromtimestamp(folder.stat().st_ctime).strftime("%Y-%m-%d") in row.label.text()
    assert folder.name not in row.label.text()
    assert panel.findChildren(QCheckBox) == []
    calls, dialogs = [], []

    def execute(dialog):
        dialogs.append(dialog)
        box = dialog.checkBox()
        assert box is not None and not box.isChecked()
        assert "Delete the newest backup too" == box.text()
        assert "kept" in dialog.text().lower()
        initial_total = 3 * MIB if summary else 0
        assert f"Total: {core.diskspace.human_size(initial_total)}" in dialog.text()
        box.setChecked(tick)
        chosen_total = initial_total + (3 * MIB if tick else 0)
        assert f"Total: {core.diskspace.human_size(chosen_total)}" in dialog.text()
        return QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", execute)
    monkeypatch.setattr(
        core, "clear_backup", lambda entry: calls.append(entry) or core.ClearResult()
    )
    monkeypatch.setattr(panel, "_start_clear", lambda work: work())
    (panel.categories["backup"].button if summary else row.button).click()
    assert len(dialogs) == 1
    expected = ([older] if summary else []) + ([newest] if tick else [])
    assert calls == (expected if accept else [])
    panel.deleteLater()


def test_empty_unknown_and_recovered(qapp, tmp_path, monkeypatch):
    from nadare.core.config import Config

    panel = DiskSpacePanel()
    unknown = core.ShaderCache("mesa", "Mesa", None, (tmp_path,), None, False)
    panel._fill_report(core.Report(shaders=(unknown,)))
    for category in panel.categories.values():
        assert "Nothing to clear" in category.label.text()
        assert not category.button.isEnabled()
        assert not category.rows
    config = Config.load(tmp_path / "config.json")
    config.notice = "recovered"
    monkeypatch.setattr(
        disk_space_panel.threading,
        "Thread",
        lambda **_: pytest.fail("recovered settings started a measurement"),
    )
    panel.load(config)
    panel.refresh()
    for category in panel.categories.values():
        assert RECOVERED_TEXT in category.label.text()
        assert not category.button.isEnabled()
    panel.deleteLater()
