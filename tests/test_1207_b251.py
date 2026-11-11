# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b251: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from nadare.core.config import Config
from nadare.gui.workers import BackendWorker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_a_malformed_media_document_degrades_instead_of_failing(qapp, tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    media = tmp_path / "media.json"
    media.write_text("{ this is not json", encoding="utf-8")
    config = Config.load(tmp_path / "config.json")

    class Ctx:
        profile = {"media_file": str(media), "title": "Game"}

    class FakeBackend:
        game_tag = "game"
        ctx = Ctx()

    worker = BackendWorker(config, "game", "media")
    assert worker._load_media(FakeBackend()) == {
        "kind": "media",
        "hero": None,
        "banners": [],
    }
