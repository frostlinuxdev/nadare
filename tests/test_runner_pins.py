# SPDX-License-Identifier: GPL-3.0-or-later
"""DW-Proton 11.0-15 is the tested pin in the runner table."""

from __future__ import annotations

from nadare.core import runners as core_runners


def test_dwproton_has_a_tested_pin():
    tag, sha = core_runners.RUNNERS["dwproton"].tested
    assert tag == "dwproton-11.0-15"
    assert sha == "8066dec3d7f532d55e5b6d697a61d6d4a3091f859a1ef9b2943895aa1b947afd"
    assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")



def _fake_release_json():
    return {
        "tag_name": "t1",
        "assets": [{"name": "dwproton-1.0-x86_64.tar.xz", "browser_download_url": "https://x"}],
    }


def test_github_token_from_the_environment_is_sent_to_the_api(monkeypatch):
    from nadare.core import runners

    seen = []

    def fetch(url, headers=None, timeout=30):
        seen.append(dict(headers or {}))
        return _fake_release_json()

    monkeypatch.setenv("NADARE_GITHUB_TOKEN", "tok123")
    monkeypatch.setattr(runners.download, "fetch_json", fetch)
    spec = runners.RUNNERS["dwproton"]
    runners._release(spec, None)
    assert seen[0].get("Authorization") == "Bearer tok123"


def test_a_rejected_github_token_falls_back_to_anonymous_and_says_so(monkeypatch, capsys):
    from nadare.core import runners

    seen = []

    def fetch(url, headers=None, timeout=30):
        seen.append(dict(headers or {}))
        if "Authorization" in (headers or {}):
            raise runners.download.DownloadError(f"HTTP 401 for {url}")
        return _fake_release_json()

    monkeypatch.setenv("NADARE_GITHUB_TOKEN", "expired")
    monkeypatch.setattr(runners.download, "fetch_json", fetch)
    assert runners._release(runners.RUNNERS["dwproton"], None)["tag"] == "t1"
    assert len(seen) == 2 and "Authorization" not in seen[1]
    assert "NADARE_GITHUB_TOKEN" in capsys.readouterr().err


def test_no_token_means_no_authorization_header(monkeypatch):
    from nadare.core import runners

    seen = []

    def fetch(url, headers=None, timeout=30):
        seen.append(dict(headers or {}))
        return _fake_release_json()

    monkeypatch.delenv("NADARE_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(runners.download, "fetch_json", fetch)
    runners._release(runners.RUNNERS["dwproton"], None)
    assert "Authorization" not in seen[0]
