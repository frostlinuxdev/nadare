# SPDX-License-Identifier: GPL-3.0-or-later
"""``wine.environ_of``: the environment a process was started with, read from ``/proc``."""

from __future__ import annotations

from nadare.backends import wine


def test_environ_of_reads_a_nul_separated_environment(tmp_path):
    entry = tmp_path / "42"
    entry.mkdir()
    (entry / "environ").write_bytes(
        b"DISPLAY=:7\0XAUTHORITY=/run/user/1000/xauth\0EMPTY=\0NO_VALUE"
    )

    assert wine.environ_of(42, proc_root=tmp_path) == {
        "DISPLAY": ":7",
        "XAUTHORITY": "/run/user/1000/xauth",
        "EMPTY": "",
        # A chunk without "=" is still a key with an empty value (as /proc can show).
        "NO_VALUE": "",
    }


def test_environ_of_is_empty_when_the_process_is_unreadable(tmp_path):
    assert wine.environ_of(42, proc_root=tmp_path) == {}
    assert wine.environ_of(42, proc_root=tmp_path / "missing") == {}


def test_winegame_reexports_environ_of():
    """Siblings are reachable through ``winegame``; this one must be too."""
    from nadare.backends import winegame

    assert winegame.environ_of is wine.environ_of
