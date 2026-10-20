# SPDX-License-Identifier: GPL-3.0-or-later
"""The "new version available" check (no network: fetch is injected)."""

from __future__ import annotations

import pytest

from nadare.core import updatecheck
from nadare.core.updatecheck import (
    UpdateInfo,
    check_latest,
    is_prerelease,
    newer,
    version_key,
)

API = "https://forge.example/api/v1/repos/o/nadare/releases/latest"


def _release(tag="v0.2.0", url="https://forge.example/o/nadare/releases/tag/v0.2.0", **kw):
    return {"tag_name": tag, "html_url": url, "name": kw.get("name", f"Nadare {tag}")}


class TestVersionKey:
    @pytest.mark.parametrize(
        ("older", "newer_"),
        [
            ("0.1.0", "0.2.0"),
            ("v0.1.0", "0.1.1"),
            ("0.9.0", "0.10.0"),
            ("1.0.0-beta.1", "1.0.0-beta.2"),
            ("1.0.0-beta.2", "1.0.0"),
            ("1.0.0-beta.9", "1.0.0-beta.10"),
            ("0.1", "0.1.1"),
        ],
    )
    def test_ordering(self, older, newer_):
        assert version_key(older) < version_key(newer_)
        assert newer(newer_, older)
        assert not newer(older, newer_)

    def test_equal_ignores_v_prefix(self):
        assert version_key("v1.2.3") == version_key("1.2.3")
        assert not newer("v1.2.3", "1.2.3")

    def test_four_component_release_is_newer(self):
        """L8: a fourth component must not parse equal to a three-component tag."""
        assert version_key("1.2.3") < version_key("1.2.3.4")
        assert newer("1.2.3.4", "1.2.3")
        assert not newer("1.2.3", "1.2.3.4")

    @pytest.mark.parametrize(
        ("pep440", "dash"),
        [
            ("0.1.0b1", "0.1.0-beta.1"),
            ("2.0rc2", "2.0-rc.2"),
            ("1.5a3", "1.5-alpha.3"),
        ],
    )
    def test_pep440_suffix_matches_dash_form(self, pep440, dash):
        """A glued PEP 440 suffix must parse identically to the dash form."""
        assert version_key(pep440) is not None
        assert version_key(pep440) == version_key(dash)

    def test_pep440_prerelease_ranks_below_the_release(self):
        assert version_key("0.1.0a1") < version_key("0.1.0b1")
        assert version_key("0.1.0b1") < version_key("0.1.0rc1")
        assert version_key("0.1.0rc1") < version_key("0.1.0")
        assert newer("0.1.0b2", "0.1.0b1")
        assert not newer("0.1.0b1", "0.1.0b1")

    @pytest.mark.parametrize("bad", ["", "latest", "v", "1..2", None, 3])
    def test_unparseable_is_none(self, bad):
        assert version_key(bad) is None
        assert not newer(bad, "0.1.0")

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("0.1.0b1", True),  # PEP 440, no dash
            ("0.1.0-beta.1", True),
            ("1.0.0rc2", True),
            ("1.5a3", True),
            ("0.1.0", False),
            ("v0.2.0", False),
            ("nightly", False),
            (None, False),
        ],
    )
    def test_is_prerelease(self, tag, expected):
        """NEW-1: "current is a prerelease" is decided from the parsed key."""
        assert is_prerelease(tag) is expected


class TestCheckLatest:
    def test_reports_a_newer_release(self):
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            return _release()

        info = check_latest("0.1.0", url=API, fetch=fetch)
        assert info == UpdateInfo(
            version="0.2.0",
            url="https://forge.example/o/nadare/releases/tag/v0.2.0",
            name="Nadare v0.2.0",
        )
        assert calls == [(API, 10)]

    def test_same_or_older_is_none(self):
        assert check_latest("0.2.0", url=API, fetch=lambda u, t: _release()) is None
        assert check_latest("0.3.0", url=API, fetch=lambda u, t: _release()) is None

    def test_disabled_url_never_fetches(self):
        def fetch(url, timeout):
            raise AssertionError("must not fetch")

        assert check_latest("0.1.0", url="", fetch=fetch) is None

    def test_default_url_comes_from_hosting(self, monkeypatch):
        monkeypatch.setenv("NADARE_RELEASES_API", API)
        seen = []
        check_latest("0.1.0", fetch=lambda u, t: seen.append(u) or _release())
        assert seen == [API]

    def test_non_https_url_is_refused(self):
        insecure = "http://forge.example/x"
        assert check_latest("0.1.0", url=insecure, fetch=lambda u, t: _release()) is None

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            {},
            {"tag_name": "v9.0.0"},  # no html_url
            {"tag_name": "v9.0.0", "html_url": "javascript:alert(1)"},
            {"tag_name": "v9.0.0", "html_url": "http://insecure.example/r"},
            {"tag_name": "nightly", "html_url": "https://forge.example/r"},
            {"tag_name": "v9.0.0", "html_url": "https://forge.example/r", "draft": True},
            {"tag_name": "v9.0.0", "html_url": "https://forge.example/r", "prerelease": True},
        ],
    )
    def test_bad_or_unwanted_payloads_are_none(self, payload):
        assert check_latest("0.1.0", url=API, fetch=lambda u, t: payload) is None

    def test_prerelease_allowed_when_current_is_prerelease(self):
        payload = {
            "tag_name": "v0.2.0-beta.2",
            "html_url": "https://forge.example/r",
            "prerelease": True,
        }
        info = check_latest("0.2.0-beta.1", url=API, fetch=lambda u, t: payload)
        assert info is not None and info.version == "0.2.0-beta.2"
        assert info.name == "v0.2.0-beta.2"  # falls back to the tag

    def test_pep440_prerelease_current_is_offered_newer_prerelease(self):
        """NEW-1: ``0.1.0b1`` has no dash but is still a prerelease."""
        payload = {
            "tag_name": "v0.1.0b2",
            "html_url": "https://forge.example/r",
            "prerelease": True,
        }
        info = check_latest("0.1.0b1", url=API, fetch=lambda u, t: payload)
        assert info is not None and info.version == "0.1.0b2"

    def test_prerelease_refused_for_plain_current(self):
        payload = {
            "tag_name": "v0.2.0b2",
            "html_url": "https://forge.example/r",
            "prerelease": True,
        }
        assert check_latest("0.1.0", url=API, fetch=lambda u, t: payload) is None

    def test_network_errors_are_swallowed(self):
        def fetch(url, timeout):
            raise OSError("offline")

        assert check_latest("0.1.0", url=API, fetch=fetch) is None

    def test_off_host_release_link_is_refused(self):
        """L9: the update link must be on the configured release host."""
        payload = {
            "tag_name": "v0.2.0",
            "html_url": "https://phishing.example/nadare",
        }
        assert check_latest("0.1.0", url=API, fetch=lambda u, t: payload) is None

    def test_github_api_may_link_to_github_release_page(self):
        api = "https://api.github.com/repos/o/r/releases/latest"
        payload = {
            "tag_name": "v0.2.0",
            "html_url": "https://github.com/o/r/releases/tag/v0.2.0",
        }
        info = check_latest("0.1.0", url=api, fetch=lambda u, t: payload)
        assert info is not None and info.url.startswith("https://github.com/")

    def test_default_fetch_uses_download_fetch_json(self, monkeypatch):
        seen = []

        def fake_fetch_json(url, *, headers=None, timeout=60):
            seen.append((url, timeout))
            return _release()

        monkeypatch.setattr(updatecheck.download, "fetch_json", fake_fetch_json)
        info = check_latest("0.1.0", url=API)
        assert info is not None and seen == [(API, 10)]

    def test_current_defaults_to_package_version(self, monkeypatch):
        import nadare

        monkeypatch.setattr(nadare, "__version__", "0.2.0")
        assert check_latest(url=API, fetch=lambda u, t: _release()) is None


def test_prerelease_labels_rank_alpha_beta_rc() -> None:
    from nadare.core.updatecheck import newer

    assert newer("v0.1.0-beta.1", "0.1.0-alpha.3")
    assert newer("v0.2.0-rc.1", "0.2.0-beta.3")
    assert not newer("v0.1.0-alpha.9", "0.1.0-beta.1")
    assert newer("v0.2.0", "0.2.0-rc.2")
