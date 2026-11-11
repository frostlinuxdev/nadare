import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])

def _pump(qapp, rounds=50):
    for _ in range(rounds):
        qapp.processEvents()
        time.sleep(0.01)

def test_a_stale_relink_answer_does_not_commit_another_games_plan(tmp_path, qapp, monkeypatch):
    old_a = tmp_path / "a-pfx"
    (old_a / "drive_c").mkdir(parents=True)
    old_b = tmp_path / "b-pfx"
    (old_b / "drive_c").mkdir(parents=True)
    new_a = tmp_path / "new-a"
    (new_a / "drive_c").mkdir(parents=True)
    new_b = tmp_path / "new-b"
    (new_b / "drive_c").mkdir(parents=True)
    gate_a, gate_b = threading.Event(), threading.Event()
    calls = {"a": 0, "b": 0}

    def probe(name):
        calls[name] += 1
        if calls[name] == 1:
            return SimpleNamespace(state="installed")
        if name == "a":
            gate_a.wait(5)
            return SimpleNamespace(state="installed")
        gate_b.wait(5)
        return SimpleNamespace(state="running")

    monkeypatch.setattr(settings_mod, "create_backend",
                        lambda profile, config: SimpleNamespace(status=lambda: probe(profile)))
    chosen = iter([str(new_a), str(new_b)])
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: next(chosen))
    shown = []
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="stove-czn", prefix=str(old_a))
    config.add_profile("b", backend="stove-czn", prefix=str(old_b))
    page = SettingsPage()
    try:
        page.load(config, "a")
        _pump(qapp)
        assert page.relink_button.isEnabled()
        page.relink_button.click()
        _pump(qapp, 10)
        assert calls["a"] == 2
        page.load(config, "b")
        _pump(qapp)
        assert page.relink_button.isEnabled()
        page.relink_button.click()
        _pump(qapp, 10)
        assert calls["b"] == 2
        gate_a.set()
        _pump(qapp, 100)
        gate_b.set()
        _pump(qapp, 100)
        assert config.profiles["b"]["prefix"] == str(old_b)
        assert any("running" in text.lower() for text in shown)
    finally:
        gate_a.set()
        gate_b.set()
        page.deleteLater()
        qapp.processEvents()
