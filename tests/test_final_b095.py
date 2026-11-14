# SPDX-License-Identifier: GPL-3.0-or-later
"""A stop request written after the lock was released must survive hold()."""
from __future__ import annotations

from nadare.core import runlock


def test_a_stop_request_made_after_the_release_is_not_deleted(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    real = runlock.fcntl  # capture before patching runlock.fcntl

    class Fcntl:
        LOCK_EX = real.LOCK_EX
        LOCK_UN = real.LOCK_UN

        @staticmethod
        def flock(fd, operation):
            real.flock(fd, operation)
            if operation == real.LOCK_UN:
                # another instance asks to stop right after this holder released the lock
                runlock.request_stop(prefix)

    monkeypatch.setattr(runlock, "fcntl", Fcntl)
    with runlock.hold(prefix):
        pass
    assert runlock.stop_requested(prefix) is True
