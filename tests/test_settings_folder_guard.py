# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings will not save a drive root, the home folder or another game's folder as a game's
Wine prefix or game folder (#851)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "user"
    folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    return folder


@pytest.fixture
def shown(monkeypatch):
    messages: list[str] = []
    for name in ("critical", "warning", "information"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            staticmethod(lambda parent, title, text, *a, _log=messages, **k: _log.append(text)),
        )
    return messages


def _window(tmp_path, home):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    good = home / "Games" / "A" / "prefix"
    good.mkdir(parents=True)
    config.add_profile("a", backend="wine", title="A", prefix=str(good))
    config.add_profile("b", backend="wine", title="B", prefix=str(home / "Games" / "B" / "prefix"))
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    return config, window


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _try(window, key, value):
    window.open_game("a")
    window.open_settings()
    window.settings._option_widgets[key].setText(str(value))
    window.settings.save()


@pytest.mark.parametrize("where", ["home", "documents", "other_game"])
def test_a_bad_prefix_is_refused_and_nothing_is_saved(tmp_path, qapp, home, shown, where):
    config, window = _window(tmp_path, home)
    try:
        before = dict(config.profiles["a"])
        bad = {
            "home": home,
            "documents": home / "Documents",
            "other_game": home / "Games" / "B" / "prefix",
        }[where]
        (home / "Documents").mkdir(exist_ok=True)
        _try(window, "prefix", bad)
        assert config.profiles["a"] == before
        assert shown and str(bad) in shown[-1]
    finally:
        _close(window, qapp)


def test_a_drive_root_is_refused(tmp_path, qapp, home, shown, monkeypatch):
    import os

    config, window = _window(tmp_path, home)
    try:
        drive = tmp_path / "drive"
        drive.mkdir()
        real = os.path.ismount
        monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(drive.resolve()) or real(p))
        _try(window, "prefix", drive)
        assert config.profiles["a"]["prefix"] != str(drive)
        assert "drive" in shown[-1]
    finally:
        _close(window, qapp)


def test_a_folder_inside_a_drive_is_accepted(tmp_path, qapp, home, shown):
    config, window = _window(tmp_path, home)
    try:
        fine = home / "Games" / "A2" / "prefix"
        _try(window, "prefix", fine)
        assert config.profiles["a"]["prefix"] == str(fine)
    finally:
        _close(window, qapp)


def test_an_existing_bad_value_does_not_block_saving_other_settings(tmp_path, qapp, home, shown):
    config, window = _window(tmp_path, home)
    try:
        config.profiles["a"]["prefix"] = str(home)  # saved earlier, before this check existed
        window.open_game("a")
        window.open_settings()
        window.settings.title.setText("Renamed")
        window.settings.save()
        assert config.profiles["a"]["title"] == "Renamed"
        assert config.profiles["a"]["prefix"] == str(home)
    finally:
        _close(window, qapp)
