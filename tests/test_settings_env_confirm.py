# SPDX-License-Identifier: GPL-3.0-or-later
"""Environment is guarded: invalid names are refused and a changed value asks first (#778)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_module
from nadare.gui.settings import SettingsPage, _text_to_env

YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def asked(monkeypatch):
    """Record every question; the answer is set per test with ``asked.answer``."""

    class Recorder(list):
        answer = YES

    recorder = Recorder()

    def question(parent, title, text, *args, **kwargs):
        recorder.append((title, text, args))
        return recorder.answer

    monkeypatch.setattr(settings_module.QMessageBox, "question", question)
    return recorder


@pytest.fixture
def page(qapp):
    widget = SettingsPage()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _config(tmp_path, **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", **profile)
    return config


@pytest.mark.parametrize("name", ["MY VAR", "1ABC", "A-B", "A.B", "é"])
def test_a_bad_variable_name_is_refused_with_a_warning(name):
    warnings: list[str] = []
    assert _text_to_env(f"{name}=1\nGOOD=2", warnings) == {"GOOD": "2"}
    assert any("variable name" in w for w in warnings)


@pytest.mark.parametrize("name", ["A", "_A", "a1", "WAYLANDDRV_PRIMARY_MONITOR"])
def test_ordinary_variable_names_pass(name):
    warnings: list[str] = []
    assert _text_to_env(f"{name}=1", warnings) == {name: "1"}
    assert warnings == []


def test_a_changed_environment_asks_and_saves_on_yes(tmp_path, page, asked):
    config = _config(tmp_path)
    page.load(config, "sample")
    page.option_widget("extra_env").setPlainText("DXVK_HUD=fps")
    page.save()
    assert len(asked) == 1
    assert "variables" in asked[0][1]
    assert config.profile("sample")["extra_env"] == {"DXVK_HUD": "fps"}


def test_declining_keeps_the_old_value(tmp_path, page, asked):
    asked.answer = NO
    config = _config(tmp_path, extra_env={"OLD": "1"})
    page.load(config, "sample")
    page.option_widget("extra_env").setPlainText("NEW=2")
    page.save()
    assert len(asked) == 1
    assert config.profile("sample")["extra_env"] == {"OLD": "1"}


def test_the_question_defaults_to_no(tmp_path, page, asked):
    page.load(_config(tmp_path), "sample")
    page.option_widget("extra_env").setPlainText("A=1")
    page.save()
    assert NO in asked[0][2]  # passed as the default button


def test_an_unchanged_environment_never_asks(tmp_path, page, asked):
    config = _config(tmp_path, extra_env={"A": "1"})
    page.load(config, "sample")
    page.save()
    assert asked == []


def test_clearing_the_environment_does_not_ask(tmp_path, page, asked):
    config = _config(tmp_path, extra_env={"A": "1"})
    page.load(config, "sample")
    page.option_widget("extra_env").setPlainText("")
    page.save()
    assert asked == []
    assert not config.profile("sample").get("extra_env")


def test_other_changes_do_not_ask(tmp_path, page, asked):
    config = _config(tmp_path)
    page.load(config, "sample")
    page.title.setText("Renamed")
    page.save()
    assert asked == []
