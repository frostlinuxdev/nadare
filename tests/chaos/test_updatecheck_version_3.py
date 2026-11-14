# SPDX-License-Identifier: GPL-3.0-or-later
"""A release tag that is not a version must be ignored, not raise from int()."""

from __future__ import annotations

from nadare.core import updatecheck

API = "https://forge.example/api/v1/repos/o/nadare/releases/latest"
TAG = "v" + "9" * 5000  # a huge numeric tag: valid JSON, not a version


def test_an_absurdly_long_numeric_release_tag_is_ignored():
    payload = {"tag_name": TAG, "html_url": "https://forge.example/r"}
    print("tag length:", len(TAG))
    try:
        key = updatecheck.version_key(TAG)
    except Exception as exc:
        key = f"{type(exc).__name__}: {exc}"
    print("version_key:", key)
    try:
        info = updatecheck.check_latest("0.1.0", url=API, fetch=lambda url, timeout: payload)
    except Exception as exc:
        info = f"{type(exc).__name__}: {exc}"
    print("check_latest:", info)
    assert key is None
    assert info is None
