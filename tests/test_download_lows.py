# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit low #594 (b) and (c): Sophon checks free space first, and duplicate paths are refused."""

from __future__ import annotations

import pytest
from test_sophon import Build, plan_for

from nadare.core import directdl as dd
from nadare.core import sophon
from nadare.core.backends import BackendError


@pytest.fixture
def servers():
    made = []

    def make(build):
        server = build.serve()
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


# ---- (c) ---------------------------------------------------------------------------------
def _plan(*paths):
    files = tuple(dd.RemoteFile(path=path, url="https://cdn.example/x") for path in paths)
    return dd.Plan("T", "1", files=files)


@pytest.mark.parametrize(
    "paths",
    [
        ("data/a.bin", "data//a.bin"),
        ("data/a.bin", "./data/a.bin"),
        ("a.bin", "a.bin"),
        ("data/./a.bin", "data/a.bin"),
    ],
)
def test_two_files_that_normalise_to_one_path_are_refused(paths):
    with pytest.raises(dd.DirectError, match="(?i)duplicate"):
        dd._validate_plan(_plan(*paths))


def test_distinct_paths_are_accepted():
    dd._validate_plan(_plan("data/a.bin", "data/b.bin", "other/a.bin"))


# ---- (b) ---------------------------------------------------------------------------------
def test_a_fresh_sophon_install_asks_for_the_space_before_writing(servers, tmp_path, monkeypatch):
    build = Build({"a.bin": b"a" * 100, "b.bin": b"b" * 50}, chunk_size=100)
    servers(build)
    calls = []
    real = sophon.diskspace.check_space

    def spy(path, needed, *args, **kwargs):
        calls.append((str(path), needed, sorted(p.name for p in tmp_path.glob("*.bin"))))
        return real(path, needed, *args, **kwargs)

    monkeypatch.setattr(sophon.diskspace, "check_space", spy)
    dd.sync(plan_for(build), tmp_path, workers=1)
    first = calls[0]
    assert first[1] == 150  # all fresh bytes in one check
    assert first[2] == []  # asked before any file was created


def test_a_full_disk_stops_a_fresh_install_before_it_creates_files(servers, tmp_path, monkeypatch):
    build = Build({"a.bin": b"a" * 100}, chunk_size=100)
    servers(build)

    def full(path, needed, *args, **kwargs):
        raise BackendError("not enough free space", 4)

    monkeypatch.setattr(sophon.diskspace, "check_space", full)
    with pytest.raises(BackendError):
        dd.sync(plan_for(build), tmp_path, workers=1)
    assert list(tmp_path.glob("*.bin")) == []


def test_an_install_that_is_already_complete_does_not_ask_for_space_again(
    servers, tmp_path, monkeypatch
):
    build = Build({"a.bin": b"a" * 100}, chunk_size=100)
    servers(build)
    dd.sync(plan_for(build), tmp_path, workers=1)
    calls = []
    monkeypatch.setattr(
        sophon.diskspace, "check_space", lambda path, needed, *a, **k: calls.append(needed)
    )
    dd.sync(plan_for(build), tmp_path, workers=1)
    assert [needed for needed in calls if needed > 0] == []
