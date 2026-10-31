# SPDX-License-Identifier: GPL-3.0-or-later
"""#770: installs promise the space they still need, so two cannot both claim the same bytes."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nadare.core import diskspace
from nadare.core.backends import BackendError

GB = 1024**3


@pytest.fixture
def disk(monkeypatch, tmp_path):
    """One fake disk: ``state['free']`` bytes free, shared by every path under tmp_path."""
    state = {"free": 100 * GB}
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: state["free"])
    monkeypatch.setattr(diskspace, "_device", lambda path: 1)
    diskspace._PROMISES.clear()
    yield state
    diskspace._PROMISES.clear()


def test_a_lone_install_is_never_refused_by_a_reservation(disk, tmp_path):
    disk["free"] = 10 * GB  # less than it needs: today's per-file checks decide that, not us
    with diskspace.reserve(tmp_path, 70 * GB, owner="a") as held:
        assert held.owner == "a"


def test_a_second_install_that_does_not_fit_beside_the_first_is_refused(disk, tmp_path):
    first = diskspace.reserve(tmp_path, 60 * GB, owner="a")
    with pytest.raises(BackendError, match="both"):
        diskspace.reserve(tmp_path, 60 * GB, owner="b")
    first.release()


def test_a_second_install_that_fits_beside_the_first_is_accepted(disk, tmp_path):
    first = diskspace.reserve(tmp_path, 30 * GB, owner="a")
    second = diskspace.reserve(tmp_path, 30 * GB, owner="b")
    second.release()
    first.release()


def test_releasing_frees_the_promise(disk, tmp_path):
    first = diskspace.reserve(tmp_path, 60 * GB, owner="a")
    first.release()
    diskspace.reserve(tmp_path, 60 * GB, owner="b").release()


def test_the_promise_shrinks_as_the_first_install_writes(disk, tmp_path):
    first = diskspace.reserve(tmp_path, 60 * GB, owner="a")
    disk["free"] = 100 * GB - 50 * GB  # 50 GB of it is now on disk; 10 GB still to come
    # 50 GB free, 10 GB still promised to "a": 40 GB left for "b".
    second = diskspace.reserve(tmp_path, 35 * GB, owner="b")
    second.release()
    first.release()


def test_other_disks_do_not_count(disk, tmp_path, monkeypatch):
    devices = {"one": 1, "two": 2}
    monkeypatch.setattr(diskspace, "_device", lambda path: devices[Path(path).name])
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = diskspace.reserve(one, 90 * GB, owner="a")
    second = diskspace.reserve(two, 90 * GB, owner="b")
    second.release()
    first.release()


def test_the_same_owner_replaces_its_own_promise(disk, tmp_path):
    diskspace.reserve(tmp_path, 60 * GB, owner="a")
    again = diskspace.reserve(tmp_path, 60 * GB, owner="a")  # a retry must not conflict with itself
    again.release()
    assert not diskspace._PROMISES


def test_the_message_says_how_much_is_promised(disk, tmp_path):
    first = diskspace.reserve(tmp_path, 60 * GB, owner="a")
    with pytest.raises(BackendError) as info:
        diskspace.reserve(tmp_path, 60 * GB, owner="b")
    text = str(info.value)
    assert "60" in text and "promised" in text
    first.release()


def test_two_threads_cannot_both_win_the_last_space(disk, tmp_path):
    results: list[str] = []
    barrier = threading.Barrier(2)

    def go(owner: str) -> None:
        barrier.wait()
        try:
            diskspace.reserve(tmp_path, 60 * GB, owner=owner)
            results.append("ok")
        except BackendError:
            results.append("refused")

    threads = [threading.Thread(target=go, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "refused"]


def test_a_stale_reservation_cannot_release_a_newer_promise_of_the_same_owner(disk, tmp_path):
    older = diskspace.reserve(tmp_path, 40 * GB, owner="a")
    newer = diskspace.reserve(tmp_path, 40 * GB, owner="a")  # replaces the first promise
    older.release()  # a late release of the old one
    assert ("a" in {owner for (_dev, owner) in diskspace._PROMISES})
    with pytest.raises(BackendError):
        diskspace.reserve(tmp_path, 60 * GB, owner="b")  # a's live promise still counts
    newer.release()
    assert not diskspace._PROMISES
