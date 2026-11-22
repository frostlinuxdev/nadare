# SPDX-License-Identifier: GPL-3.0-or-later
"""A ``--config`` path the OS refuses is a concise error, not a traceback (user sim r3b07 F1).

``nadare -c <300 characters> list`` crashed with ``OSError: [Errno 36] File name too long``
before the normal error handling, because ``Path.exists()`` raises for that errno.
"""

from __future__ import annotations

import pytest

from nadare import app


def test_overlong_config_name_is_a_plain_error(tmp_path, capsys):
    long_name = tmp_path / ("9" * 300)
    code = app.run_cli(["-c", str(long_name), "list"])
    err = capsys.readouterr().err
    assert code == 2
    assert "[error]" in err
    assert "Traceback" not in err
    assert "too long" in err.lower()


@pytest.mark.parametrize("part", ["a" * 300, "b" * 256])
def test_overlong_directory_part_is_a_plain_error(tmp_path, capsys, part):
    code = app.run_cli(["-c", str(tmp_path / part / "config.json"), "list"])
    assert code == 2
    assert "[error]" in capsys.readouterr().err


def test_missing_config_message_is_unchanged(tmp_path, capsys):
    code = app.run_cli(["-c", str(tmp_path / "nope.json"), "list"])
    assert code == 2
    assert "config file not found" in capsys.readouterr().err
