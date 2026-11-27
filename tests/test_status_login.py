# SPDX-License-Identifier: GPL-3.0-or-later
"""`nadare status` reports whether the profile has a login, when the backend knows."""

from __future__ import annotations

import argparse

import pytest

from nadare import app
from nadare.core.backends import Status


class _Backend:
    def __init__(self, data: dict, version: str = "v55") -> None:
        self._data = data
        self._version = version

    def requires_notice_ack(self) -> bool:
        return False

    def status(self) -> Status:
        return Status("installed", version=self._version, data=self._data)


def _status(monkeypatch, data: dict, version: str = "v55") -> None:
    monkeypatch.setattr(
        app, "create_backend", lambda *a, **k: _Backend(data, version)
    )
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


def test_status_names_a_download_package_version(monkeypatch, capsys):
    """HBR's number is the package Nadare downloads; the CLI must say so too."""
    _status(monkeypatch, {"version_kind": "download"}, version="4.0.3")
    out = capsys.readouterr().out
    assert "CZN-box: installed download 4.0.3" in out


def test_status_prints_a_game_build_without_the_download_word(monkeypatch, capsys):
    _status(monkeypatch, {}, version="4.0.3")
    out = capsys.readouterr().out
    assert "CZN-box: installed 4.0.3" in out
    assert "download" not in out
