# SPDX-License-Identifier: GPL-3.0-or-later
"""WebView2 counts as set up only when Windows also knows about it (the key apps look up)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import webview2

KEY = (
    r"[Software\\Wow6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
    r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}]"
)


def _files(prefix: Path, version: str = "156.0.4314.58") -> None:
    app = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application" / version
    app.mkdir(parents=True)
    (app / "msedgewebview2.exe").write_bytes(b"MZ")


def _reg(prefix: Path, pv: str | None = "156.0.4314.58", *, key: str = KEY) -> None:
    body = f'\n{key} 1793458567\n#time=1dd6947f56ae7fa\n"location"="C:\\\\x"\n'
    if pv is not None:
        body += f'"pv"="{pv}"\n'
    body += '"name"="Microsoft Edge WebView2 Runtime"\n\n[Software\\\\Other] 1\n"pv"="9.9.9.9"\n'
    (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n" + body)


def test_files_and_the_registry_key_together_are_a_working_install(tmp_path):
    _files(tmp_path)
    _reg(tmp_path)
    assert webview2.installed(tmp_path) and webview2.registered(tmp_path)


def test_files_without_the_key_are_a_half_install(tmp_path):
    _files(tmp_path)
    assert webview2.installed(tmp_path) and not webview2.registered(tmp_path)  # no system.reg
    (tmp_path / "system.reg").write_text("WINE REGISTRY Version 2\n[Software\\\\Other] 1\n")
    assert not webview2.registered(tmp_path)


@pytest.mark.parametrize("pv", [None, "", "0.0.0.0", "  "])
def test_a_key_without_a_real_version_does_not_count(tmp_path, pv):
    _files(tmp_path)
    _reg(tmp_path, pv)
    assert not webview2.registered(tmp_path)


def test_a_version_whose_folder_is_missing_does_not_count(tmp_path):
    _files(tmp_path, "150.0.1")  # files of another version
    _reg(tmp_path, "156.0.4314.58")
    assert not webview2.registered(tmp_path)


def test_another_keys_pv_is_not_mistaken_for_it(tmp_path):
    _files(tmp_path, "9.9.9.9")
    _reg(tmp_path, None)  # our key has no pv; the NEXT section does, with a folder that exists
    assert not webview2.registered(tmp_path)


def test_the_key_text_inside_another_value_is_not_the_key(tmp_path):
    _files(tmp_path)
    (tmp_path / "system.reg").write_text(
        'WINE REGISTRY Version 2\n[Software\\\\X] 1\n"note"="' + KEY + '"\n"pv"="156.0.4314.58"\n'
    )
    assert not webview2.registered(tmp_path)


def test_a_stray_bad_byte_elsewhere_does_not_hide_a_good_registration(tmp_path):
    _files(tmp_path)
    _reg(tmp_path)
    data = (tmp_path / "system.reg").read_bytes()
    (tmp_path / "system.reg").write_bytes(b'[Software\\\\Y] 1\n"v"="caf\xe9"\n' + data)
    assert webview2.registered(tmp_path)


def test_a_missing_registry_file_is_just_not_registered(tmp_path):
    _files(tmp_path)
    assert not webview2.registered(tmp_path)
