# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-app Windows version in a prefix's user.reg (HKCU\\Software\\Wine\\AppDefaults)."""

from __future__ import annotations

from nadare.core import winereg

KEY = "[Software\\\\Wine\\\\AppDefaults\\\\game.exe]"


def _user_reg(tmp_path, body=""):
    path = tmp_path / "user.reg"
    path.write_text(
        "WINE REGISTRY Version 2\n;; All keys relative to \\\\User\\\\S-1-5-21-0\n\n#arch=win64\n"
        + body
    )
    return path


def test_adds_the_key_when_missing(tmp_path):
    path = _user_reg(tmp_path)

    assert winereg.set_app_version(tmp_path, "game.exe", "win7") is True

    text = path.read_text()
    assert KEY in text
    block = text[text.index(KEY) :]
    assert '"Version"="win7"' in block
    assert text.startswith("WINE REGISTRY Version 2\n")


def test_second_call_changes_nothing(tmp_path):
    path = _user_reg(tmp_path)
    winereg.set_app_version(tmp_path, "game.exe", "win7")
    before = path.read_text()

    assert winereg.set_app_version(tmp_path, "game.exe", "win7") is False
    assert path.read_text() == before


def test_replaces_another_version_and_keeps_other_values(tmp_path):
    path = _user_reg(
        tmp_path,
        f'\n{KEY} 1\n#time=1\n"Version"="win10"\n"Other"="x"\n'
        '\n[Software\\\\Next] 1\n"Version"="keep"\n',
    )

    assert winereg.set_app_version(tmp_path, "game.exe", "win7") is True

    text = path.read_text()
    assert text.count('"Version"="win7"') == 1
    assert '"Other"="x"' in text
    assert '"Version"="keep"' in text
    assert '"Version"="win10"' not in text


def test_adds_the_value_to_an_existing_key(tmp_path):
    path = _user_reg(tmp_path, f'\n{KEY} 1\n#time=1\n"Other"="x"\n')

    assert winereg.set_app_version(tmp_path, "game.exe", "win7") is True

    text = path.read_text()
    assert text.count(KEY) == 1
    assert '"Version"="win7"' in text and '"Other"="x"' in text


def test_does_nothing_without_user_reg(tmp_path):
    assert winereg.set_app_version(tmp_path, "game.exe", "win7") is False
    assert not (tmp_path / "user.reg").exists()
