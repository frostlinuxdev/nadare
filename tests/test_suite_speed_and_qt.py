# SPDX-License-Identifier: GPL-3.0-or-later
"""Guards for the parallel-friendly test suite and the font-change refresh."""

from __future__ import annotations

import time

import _httpserver
import pytest

from nadare.gui import theme, workers

# This file restyles the whole app: end earlier tests' windows first (#1024).
pytestmark = pytest.mark.usefixtures("restyle_safe")


def test_the_test_http_server_stops_quickly():
    server = _httpserver.Server({"a": b"x"})
    start = time.monotonic()
    server.close()
    assert time.monotonic() - start < 0.2


def test_a_font_change_refreshes_the_stylesheet_later_and_only_once(qapp, monkeypatch):
    from PyQt6.QtGui import QFont

    original = QFont(qapp.font())
    calls: list[int] = []
    real = theme.stylesheet
    monkeypatch.setattr(theme, "stylesheet", lambda: calls.append(1) or real())
    try:
        theme.apply_theme(qapp)
        calls.clear()
        for size in (11, 12, 13):
            font = QFont(qapp.font())
            font.setPointSizeF(size)
            qapp.setFont(font)
        assert calls == []  # nothing runs inside the font-change signal
        qapp.processEvents()
        qapp.processEvents()
        assert len(calls) == 1  # three changes, one refresh
    finally:
        qapp.setFont(original)
        qapp.setStyleSheet("")
        qapp.processEvents()


class _Stuck:
    def wait(self, _ms=0):
        return False

    def isRunning(self):
        return True


def test_a_leaked_fake_worker_is_present_in_this_test():
    workers._detached.add(_Stuck())
    assert workers._detached


def test_the_next_test_does_not_inherit_a_leaked_fake_worker():
    assert not workers._detached
