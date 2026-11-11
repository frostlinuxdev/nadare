# SPDX-License-Identifier: GPL-3.0-or-later
"""Stop never signals a PID whose identity Nadare could not record (launch-path review 12)."""

from __future__ import annotations

from nadare.backends import wine
from nadare.core import process


def _kills(monkeypatch):
    sent: list[int] = []
    monkeypatch.setattr(wine.os, "kill", lambda pid, sig: sent.append(pid))
    return sent


def test_an_unrecorded_start_time_is_not_signalled(monkeypatch):
    sent = _kills(monkeypatch)
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: None)
    wine.remember_start([4242])  # unreadable at scan: it had probably just exited
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "999")  # a new process got it
    wine.stop_pids([4242])
    assert sent == []


def test_a_second_stop_still_checks_identity(monkeypatch):
    sent = _kills(monkeypatch)
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "100")
    wine.remember_start([4243])
    wine.stop_pids([4243])
    assert sent == [4243]
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "200")  # recycled
    wine.stop_pids([4243])
    assert sent == [4243]


def test_the_same_process_is_signalled(monkeypatch):
    sent = _kills(monkeypatch)
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "100")
    wine.remember_start([4244])
    wine.stop_pids([4244])
    assert sent == [4244]


def test_the_records_stay_bounded(monkeypatch):
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "1")
    monkeypatch.setattr(wine, "_SCAN_START", {})
    wine.remember_start(list(range(1, wine._SCAN_START_MAX + 50)))
    assert len(wine._SCAN_START) == wine._SCAN_START_MAX
    assert wine._SCAN_START_MAX + 49 in wine._SCAN_START  # the newest are kept
