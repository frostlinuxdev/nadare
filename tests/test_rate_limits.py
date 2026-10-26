# SPDX-License-Identifier: GPL-3.0-or-later
"""#522: installs and the update check must survive GitHub's anonymous API rate limit."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nadare.core import paths, runners, updatecheck

GH_API = "https://api.github.com/repos/frostlinuxdev/nadare/releases/latest"
GH_PAGE = "https://github.com/frostlinuxdev/nadare/releases/latest"
GH_TAG = "https://github.com/frostlinuxdev/nadare/releases/tag/v0.2.0"
HOUR = 3600.0


# --------------------------------------------------------------- runner installs
def test_every_tested_runner_pins_its_asset_name_and_size() -> None:
    for spec in runners.RUNNERS.values():
        assert spec.tested is not None and spec.pinned_asset is not None, spec.id
        name, size = spec.pinned_asset
        assert re.fullmatch(spec.asset, name), spec.id
        assert size > 0 and re.fullmatch(r"[A-Za-z0-9._-]+", name)


def _no_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("the GitHub API must not be used for a pinned install")

    monkeypatch.setattr(runners.download, "fetch_json", boom)
    monkeypatch.setattr(runners, "_release", boom)


@pytest.mark.parametrize(
    ("runner_id", "url"),
    [
        (
            "dwproton",
            "https://github.com/dawn-winery/dwproton-mirror/releases/download/"
            "dwproton-11.0-15/dwproton-11.0-15-x86_64.tar.xz",
        ),
        (
            "ge",
            "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
            "GE-Proton11-7/GE-Proton11-7-x86_64.tar.gz",
        ),
        (
            "cachyos",
            "https://github.com/CachyOS/proton-cachyos/releases/download/"
            "cachyos-11.0-20260703-slr/proton-cachyos-11.0-20260703-slr-x86_64.tar.xz",
        ),
    ],
)
def test_a_pinned_install_downloads_from_the_release_url_without_the_api(
    runner_id, url, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    monkeypatch.setattr(runners.paths, "cache_dir", lambda: tmp_path / "cache")
    _no_api(monkeypatch)
    seen: dict = {}

    def fake_download(got_url, dest, **kwargs):
        seen.update(kwargs, url=got_url, dest=dest)
        dest.write_bytes(b"x")
        return runners.download.DownloadResult(dest, 1, "b" * 64, "c" * 128)

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners, "_extract", lambda *a, **k: tmp_path / "runner")
    spec = runners.RUNNERS[runner_id]
    runners.install(runner_id)
    assert seen["url"] == url
    assert seen["expected_sha256"] == spec.tested[1]
    assert seen["expected_size"] == spec.pinned_asset[1]
    assert seen["dest"].name == spec.pinned_asset[0]
    assert seen["redirect_allowed"]("https://release-assets.githubusercontent.com/x")
    assert not seen["redirect_allowed"]("https://evil.example/x")


def test_latest_installs_still_use_the_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    monkeypatch.setattr(runners.paths, "cache_dir", lambda: tmp_path / "cache")
    called: list[str] = []

    def fake_release(spec, tag, **k):
        called.append(spec.id)
        raise runners.RunnerError("stop here")

    monkeypatch.setattr(runners, "_release", fake_release)
    with pytest.raises(runners.RunnerError):
        runners.install("ge", latest=True)
    assert called == ["ge"]


# ------------------------------------------------------------------ update check
def _payload(tag="v0.2.0"):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/frostlinuxdev/nadare/releases/tag/{tag}",
        "name": f"Nadare {tag}",
    }


@pytest.fixture
def cache_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    return tmp_path / "cache"


class _Fetch:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, url, timeout):
        self.calls += 1
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(value, Exception):
            raise value
        return value


def _check(fetch, now, redirect=None, current="0.1.0"):
    return updatecheck.check_latest(
        current,
        url=GH_API,
        cached_fetch=fetch,
        redirect=redirect or (lambda url: None),
        now=now,
    )


def test_a_good_answer_is_cached_for_hours(cache_home) -> None:
    fetch = _Fetch(_payload())
    first = _check(fetch, 1000.0)
    second = _check(fetch, 1000.0 + 5 * HOUR)
    assert first == second and first.version == "0.2.0"
    assert fetch.calls == 1
    assert json.loads((cache_home / "update-check.json").read_text())["payload"]["tag_name"]


def test_the_cache_expires_after_six_hours(cache_home) -> None:
    fetch = _Fetch(_payload("v0.2.0"), _payload("v0.3.0"))
    assert _check(fetch, 1000.0).version == "0.2.0"
    assert _check(fetch, 1000.0 + 6 * HOUR + 1).version == "0.3.0"
    assert fetch.calls == 2


def test_a_failure_backs_off_for_an_hour_and_keeps_the_last_good_answer(cache_home) -> None:
    fetch = _Fetch(_payload(), OSError("HTTP 403"), _payload("v0.3.0"))
    assert _check(fetch, 1000.0).version == "0.2.0"
    stale = _check(fetch, 1000.0 + 7 * HOUR)  # expired -> tries -> 403 -> stale answer
    assert stale is not None and stale.version == "0.2.0" and fetch.calls == 2
    assert _check(fetch, 1000.0 + 7 * HOUR + 600).version == "0.2.0"  # inside the backoff
    assert fetch.calls == 2
    assert _check(fetch, 1000.0 + 7 * HOUR + HOUR + 1).version == "0.3.0"  # backoff over
    assert fetch.calls == 3


def test_a_first_ever_failure_returns_none_and_still_backs_off(cache_home) -> None:
    fetch = _Fetch(OSError("HTTP 403"))
    assert _check(fetch, 1000.0) is None
    assert _check(fetch, 1000.0 + 600) is None
    assert fetch.calls == 1


def test_when_the_api_fails_the_release_page_redirect_answers(cache_home) -> None:
    fetch = _Fetch(OSError("HTTP 403 rate limit exceeded"))
    asked: list[str] = []

    def redirect(url):
        asked.append(url)
        return GH_TAG

    info = _check(fetch, 1000.0, redirect=redirect)
    assert asked == [GH_PAGE]
    assert info is not None and info.version == "0.2.0" and info.url == GH_TAG
    # and the answer counts as a success: no new request for hours
    assert _check(fetch, 1000.0 + HOUR, redirect=redirect).version == "0.2.0"
    assert asked == [GH_PAGE] and fetch.calls == 1


@pytest.mark.parametrize(
    "location",
    [
        None,
        "https://evil.example/frostlinuxdev/nadare/releases/tag/v9.9.9",
        "https://github.com/someone-else/nadare/releases/tag/v9.9.9",
        "http://github.com/frostlinuxdev/nadare/releases/tag/v9.9.9",
        "https://github.com/frostlinuxdev/nadare/releases/tag/not-a-version",
        "https://github.com/frostlinuxdev/nadare/releases/tag/v9.9.9/../../x",
    ],
)
def test_a_redirect_to_anywhere_else_is_ignored(cache_home, location) -> None:
    fetch = _Fetch(OSError("HTTP 403"))
    assert _check(fetch, 1000.0, redirect=lambda url: location) is None


def test_the_redirect_fallback_is_only_for_github_urls(cache_home) -> None:
    asked: list[str] = []
    fetch = _Fetch(OSError("down"))
    updatecheck.check_latest(
        "0.1.0",
        url="https://forge.example/api/v1/repos/o/nadare/releases/latest",
        cached_fetch=fetch,
        redirect=lambda url: asked.append(url),
        now=1.0,
    )
    assert asked == []


def test_a_corrupt_cache_file_is_ignored(cache_home) -> None:
    cache_home.mkdir(parents=True)
    (cache_home / "update-check.json").write_text("{not json")
    assert _check(_Fetch(_payload()), 1000.0).version == "0.2.0"


def test_a_cache_for_another_url_is_not_reused(cache_home) -> None:
    fetch = _Fetch(_payload())
    _check(fetch, 1000.0)
    other_fetch = _Fetch(_payload("v0.5.0"))
    other = updatecheck.check_latest(
        "0.1.0",
        url="https://api.github.com/repos/someone/else/releases/latest",
        cached_fetch=other_fetch,
        redirect=lambda url: None,
        now=1000.0 + 60,
    )
    assert other_fetch.calls == 1 and other is not None and other.version == "0.5.0"


def test_a_cached_payload_cannot_smuggle_a_foreign_link(cache_home) -> None:
    cache_home.mkdir(parents=True)
    (cache_home / "update-check.json").write_text(
        json.dumps(
            {
                "url": GH_API,
                "ok_at": 1000.0,
                "failed_at": None,
                "payload": {**_payload(), "html_url": "https://evil.example/x"},
            }
        )
    )
    assert _check(_Fetch(_payload()), 1100.0) is None


def test_injected_fetch_never_touches_the_disk_cache(cache_home) -> None:
    info = updatecheck.check_latest("0.1.0", url=GH_API, fetch=lambda url, timeout: _payload())
    assert info is not None and not cache_home.exists()


def test_the_real_redirect_reader_returns_the_location_without_following(monkeypatch) -> None:
    import urllib.error

    class Opener:
        def open(self, request, timeout=None):
            raise updatecheck._Redirect(GH_TAG)

    monkeypatch.setattr(updatecheck.urllib.request, "build_opener", lambda *a: Opener())
    assert updatecheck._github_latest_redirect(GH_PAGE) == GH_TAG

    class Failing:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("down")

    monkeypatch.setattr(updatecheck.urllib.request, "build_opener", lambda *a: Failing())
    assert updatecheck._github_latest_redirect(GH_PAGE) is None
