# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: a filesystem without flock must not look 'busy' forever.

``core.process`` already treats ENOLCK/ENOTSUP from flock as 'cannot tell' and
carries on.  ``directdl._install_lock`` treats every OSError as 'another install
is already running', so a home directory where flock is unsupported can never
install or update a direct game.
"""

from __future__ import annotations

import errno

from nadare.core import directdl as dd
from nadare.core import installlock


def test_an_unsupported_flock_does_not_block_every_direct_sync(tmp_path, monkeypatch):
    def no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(installlock.fcntl, "flock", no_locks)

    print("calling sync on a filesystem that cannot flock...")
    result = dd.sync(dd.Plan("T", "1"), tmp_path / "install")
    print("sync result:", result)
    assert result == {"downloaded": 0, "skipped": 0}
