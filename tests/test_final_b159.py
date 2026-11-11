import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_switch_is_locked_for_an_already_running_game(qapp):
    view = GameView()
    try:
        view.set_profile("sample", {"backend": "direct-pgr", "title": "Sample"})
        view.set_status(Status("running", running=True))
        view.set_sandbox(True, True)
        assert not view.sandbox.isEnabled(), (
            "a game that is already running must lock the Sandbox switch"
        )
    finally:
        view.deleteLater()
        qapp.processEvents()
