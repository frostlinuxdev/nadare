from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core.backends import Status
from nadare.gui.sidebar import is_installed


def test_a_running_status_counts_as_installed_even_with_another_state_word():
    status = Status(state="not_installed", running=True)
    assert is_installed(status) is True


def test_a_plain_not_installed_status_is_still_false():
    assert is_installed(Status(state="not_installed")) is False
