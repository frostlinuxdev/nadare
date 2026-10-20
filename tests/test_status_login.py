# SPDX-License-Identifier: GPL-3.0-or-later
"""`nadare status` reports whether the profile has a login, when the backend knows."""

from __future__ import annotations

import argparse

import pytest

from nadare import app
from nadare.core.backends import Status


class _Backend:
    def __init__(self, data: dict) -> None:
        self._data = data

    def requires_notice_ack(self) -> bool:
        return False

    def status(self) -> Status:
        return Status("installed", version="v55", data=self._data)


def _status(monkeypatch, data: dict) -> None:
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _Backend(data))
    args = argparse.Namespace(command="status")
    assert app._run_backend_command(args, None, "CZN-box", None, lambda event: None) == 0


@pytest.mark.parametrize(
    ("logged_in", "expected"), [(True, "login: logged in"), (False, "login: not logged in")]
)
def test_status_prints_login_line(monkeypatch, capsys, logged_in, expected):
    _status(monkeypatch, {"logged_in": logged_in})
    out = capsys.readouterr().out
    assert "CZN-box: installed v55" in out
    assert expected in out


def test_status_omits_login_line_for_backends_without_accounts(monkeypatch, capsys):
    _status(monkeypatch, {})
    assert "login:" not in capsys.readouterr().out
