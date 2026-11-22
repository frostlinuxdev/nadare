# SPDX-License-Identifier: GPL-3.0-or-later
"""#1558 item 2: the last hand-rolled atomic writers go through ``core.atomicfile.write``.

The winegame JSON writer did not fsync before the rename, so a crash could leave an empty
file; one shared writer keeps fsync, temp-file cleanup and permissions the same everywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.backends.winegame_parts import shared as winegame_shared
from nadare.core import atomicfile, sophon


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[Path, object]] = []
    real = atomicfile.write

    def spy(path, data, **kwargs):
        seen.append((Path(path), data))
        return real(path, data, **kwargs)

    monkeypatch.setattr(atomicfile, "write", spy)
    return seen


def test_winegame_json_goes_through_the_shared_writer(tmp_path, calls):
    target = tmp_path / "sub" / "state.json"
    winegame_shared._atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p for p, _ in calls] == [target]


def test_winegame_json_syncs_before_the_rename(tmp_path, monkeypatch):
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    winegame_shared._atomic_write_json(tmp_path / "s.json", {"a": 1})
    assert synced


def test_winegame_json_failure_keeps_the_old_file_and_no_temp(tmp_path):
    folder = tmp_path / "profile"
    folder.mkdir()
    target = folder / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        winegame_shared._atomic_write_json(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert sorted(p.name for p in folder.iterdir()) == ["state.json"]


def test_sophon_text_goes_through_the_shared_writer(tmp_path, calls):
    target = tmp_path / "ledger.txt"
    sophon._atomic_write_text(target, "a\r\nb\n")
    assert target.read_bytes() == b"a\r\nb\n"
    assert [p for p, _ in calls] == [target]


def test_sophon_text_creates_the_folder(tmp_path):
    target = tmp_path / "new" / "ledger.txt"
    sophon._atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"
