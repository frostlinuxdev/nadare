# SPDX-License-Identifier: GPL-3.0-or-later
"""Release metadata must reject malformed hashes and unrelated download hosts."""

from __future__ import annotations

import pytest

from nadare.core import selfupdate, updatecheck
from nadare.core.updatecheck import UpdateInfo

INFO = UpdateInfo(
    "0.1.0b24", "https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b24", "Test"
)


@pytest.mark.parametrize(
    "digest", [None, 1, [], {}, "", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", "a" * 64 + " "]
)
def test_invalid_index_checksum_is_never_accepted(digest):
    doc = {"releases": {INFO.version: {"x86_64": {"sha256": digest}}}}
    with pytest.raises(selfupdate.SelfUpdateError):
        selfupdate.expected_sha256(INFO, fetch_index=lambda: doc, fetch_sums=lambda u: "")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/frostlinuxdev/nadare",
        "https://github.com.evil.invalid/file",
        "https://github.com@evil.invalid/file",
        "file:///tmp/file",
        "javascript:alert(1)",
        "https://evil.invalid/github.com/file",
    ],
)
def test_update_download_allowlist_rejects_unrelated_hosts(url):
    assert not selfupdate.download_host_allowed(url)


@pytest.mark.parametrize("digest", ["a" * 64, "A" * 64, "0123456789abcdef" * 4])
def test_valid_index_checksum_and_crosscheck_agree(digest):
    urls = selfupdate.asset_urls(INFO)
    doc = {"releases": {INFO.version: {"x86_64": {"sha256": digest}}}}
    result = selfupdate.expected_sha256(
        INFO, fetch_index=lambda: doc, fetch_sums=lambda u: digest + "  " + urls.filename
    )
    assert result.sha256 == digest.lower()


@pytest.mark.parametrize(
    "tag", ["9" * 10000, "0." + "9" * 10000, None, [], {}, "1.0.0-rc" + "9" * 10000]
)
def test_hostile_version_is_rejected_without_integer_conversion_crash(tag):
    assert updatecheck.version_key(tag) is None
