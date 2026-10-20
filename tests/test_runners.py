# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.runners (local discovery only; no network)."""

from __future__ import annotations

import os
import tarfile
import time
from pathlib import Path

import pytest

from nadare.core import paths, runners


@pytest.fixture
def fake_runners_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(paths, "runners_dir", lambda: root)
    return root


def _make_runner(
    root: Path, name: str, *, layout: str = "proton", version: str | None = None
) -> Path:
    runner = root / name
    runner.mkdir(parents=True)
    if layout == "proton":
        (runner / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
        (runner / "proton").chmod(0o755)
    else:  # files/bin/wine layout
        wine = runner / "files" / "bin" / "wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/sh\n", encoding="utf-8")
        wine.chmod(0o755)
    if version is not None:
        (runner / "version").write_text(version + "\n", encoding="utf-8")
    return runner


def test_discovery_finds_executable_runner(fake_runners_dir: Path) -> None:
    runner = _make_runner(fake_runners_dir, "GE-Proton9-1", version="GE-Proton9-1")
    (fake_runners_dir / "not-a-runner").mkdir()  # dir without proton/wine
    (fake_runners_dir / "plain-file").write_text("x", encoding="utf-8")

    found = runners.nadare_runners()

    assert found == [runner]
    assert "GE-Proton9-1" in [p.name for p in found]


def test_discovery_accepts_wine_layout(fake_runners_dir: Path) -> None:
    runner = _make_runner(fake_runners_dir, "custom", layout="wine")
    assert runners.nadare_runners() == [runner]
    assert runners.wine_binary(runner) == runner / "files" / "bin" / "wine"
    assert runners.proton_binary(runner) == runner / "proton"


def test_runner_version_falls_back_to_dir_name(fake_runners_dir: Path) -> None:
    runner = _make_runner(fake_runners_dir, "NoVersion")
    assert runners.runner_version(runner) == "NoVersion"

    versioned = _make_runner(fake_runners_dir, "Versioned", version="v1.2.3")
    assert runners.runner_version(versioned) == "v1.2.3"


def test_resolve_runner_by_name_and_latest(fake_runners_dir: Path) -> None:
    first = _make_runner(fake_runners_dir, "A")
    second = _make_runner(fake_runners_dir, "B")

    assert runners.resolve_runner("A") == first
    # "latest"/"auto" pick the last of the sorted local list.
    assert runners.resolve_runner("latest") == second
    assert runners.resolve_runner("auto") == second
    assert runners.resolve_runner(None) is None
    assert runners.resolve_runner("") is None


def test_resolve_unknown_runner_raises(fake_runners_dir: Path) -> None:
    _make_runner(fake_runners_dir, "OnlyOne")
    with pytest.raises(runners.RunnerError):
        runners.resolve_runner("Missing")


def test_resolve_latest_without_runners_raises(fake_runners_dir: Path) -> None:
    with pytest.raises(runners.RunnerError):
        runners.resolve_runner("latest")


def test_list_runners_reports_source(fake_runners_dir: Path) -> None:
    _make_runner(fake_runners_dir, "GE-Proton9", version="9")

    rows = runners.list_runners()

    assert len(rows) == 1
    assert rows[0]["name"] == "GE-Proton9"
    assert rows[0]["version"] == "9"
    assert rows[0]["source"] == "nadare"
    assert rows[0]["path"] == str(fake_runners_dir / "GE-Proton9")


def test_remove_only_within_runners_dir(fake_runners_dir: Path, tmp_path: Path) -> None:
    runner = _make_runner(fake_runners_dir, "Removable")
    assert runners.remove("Removable") is True
    assert not runner.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    assert runners.remove(outside) is False


def test_runner_specs_have_valid_shape() -> None:
    for runner_id, spec in runners.RUNNERS.items():
        assert spec.id == runner_id
        assert spec.repo
        assert spec.asset
        if spec.tested is not None:
            tag, digest = spec.tested
            assert tag
            assert len(digest) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# upstream freshness (fake release JSON; no network)
# ---------------------------------------------------------------------------
def _release_json(tag: str, published: str, *, digest: str | None = None) -> dict:
    asset: dict = {
        "name": f"{tag}-x86_64.tar.gz",
        "size": 1024,
        "browser_download_url": (
            f"https://github.com/nadare-test/{tag}/releases/download/{tag}/{tag}-x86_64.tar.gz"
        ),
    }
    if digest:
        asset["digest"] = f"sha256:{digest}"
    return {"tag_name": tag, "published_at": published, "assets": [asset]}


@pytest.fixture(autouse=True)
def _clear_release_cache() -> None:
    runners.clear_release_cache()


def test_runner_info_flags_stale_and_outdated(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _make_runner(fake_runners_dir, "GE-Proton11-7", version="GE-Proton11-7")
    monkeypatch.setattr(
        runners.download,
        "fetch_json",
        lambda *a, **k: _release_json("GE-Proton11-9", "2020-01-01T00:00:00Z"),
    )
    info = runners.runner_info(runner)
    assert info["family"] == "ge"
    assert info["latest_tag"] == "GE-Proton11-9"
    assert info["stale"] is True
    assert info["outdated"] is True


def test_runner_info_fresh_runner_is_not_stale(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    runner = _make_runner(fake_runners_dir, "GE-Proton11-9", version="GE-Proton11-9")
    now = datetime.now(UTC).isoformat()
    monkeypatch.setattr(
        runners.download, "fetch_json", lambda *a, **k: _release_json("GE-Proton11-9", now)
    )
    info = runners.runner_info(runner)
    assert info["stale"] is False
    assert info["outdated"] is False


def test_runner_info_reads_real_proton_version_file(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Proton ``version`` files are ``<unix time> <name>``; compare the name."""
    from datetime import UTC, datetime

    runner = _make_runner(fake_runners_dir, "Proton-GE Latest", version="1789520217 GE-Proton11-7")
    now = datetime.now(UTC).isoformat()
    monkeypatch.setattr(
        runners.download, "fetch_json", lambda *a, **k: _release_json("GE-Proton11-7", now)
    )
    info = runners.runner_info(runner)
    assert info["tag"] == "GE-Proton11-7"
    assert info["outdated"] is False


def test_runner_info_offline_is_unknown(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _make_runner(fake_runners_dir, "GE-Proton11-7", version="GE-Proton11-7")

    def _boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(runners.download, "fetch_json", _boom)
    info = runners.runner_info(runner)
    assert info["latest_tag"] is None
    assert info["latest_published"] is None
    assert info["stale"] is None


def test_unknown_runner_family_is_tolerated(fake_runners_dir: Path) -> None:
    runner = _make_runner(fake_runners_dir, "CustomWine", version="1")
    info = runners.runner_info(runner)
    assert info["family"] is None
    assert info["latest_tag"] is None


def test_stale_suggestion_names_newer_ge(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str, **k):
        if "cachyos" in url:
            return _release_json("cachyos-11.0", "2025-01-01T00:00:00Z")
        return _release_json("GE-Proton11-9", "2026-01-01T00:00:00Z")

    monkeypatch.setattr(runners.download, "fetch_json", fake)
    hint = runners.stale_suggestion(
        {
            "stale": True,
            "family": "cachyos",
            "latest_published": "2025-01-01T00:00:00Z",
            "name": "Proton-CachyOS",
        }
    )
    assert hint is not None
    assert "GE-Proton11-9" in hint


# ---------------------------------------------------------------------------
# integrity: GitHub release-asset digest fallback
# ---------------------------------------------------------------------------
def test_install_uses_asset_digest_without_sha512sum(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "a" * 64
    monkeypatch.setattr(
        runners.download,
        "fetch_json",
        lambda *a, **k: _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z", digest=digest),
    )
    captured: dict = {}

    def fake_download(url, dest, **kwargs):
        captured.update(kwargs)
        dest.write_bytes(b"x")
        return runners.download.DownloadResult(dest, 1, "b" * 64, "c" * 128)

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners, "_extract", lambda *a, **k: tmp_path / "runner")
    runners.install("ge", latest=True)
    assert captured["expected_sha256"] == digest
    assert captured["expected_sha512"] is None


def test_install_checks_disk_space(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "a" * 64
    payload = _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z", digest=digest)
    monkeypatch.setattr(runners.download, "fetch_json", lambda *a, **k: payload)

    def fake_download(url, dest, **kwargs):
        dest.write_bytes(b"x")
        return runners.download.DownloadResult(dest, 1, "b" * 64, "c" * 128)

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners, "_extract", lambda *a, **k: tmp_path / "runner")
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        runners.diskspace,
        "check_space",
        lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
    )
    runners.install("ge", latest=True)
    assert calls and calls[0][1] == 1024


def test_install_refuses_without_any_checksum(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runners.download,
        "fetch_json",
        lambda *a, **k: _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(runners.RunnerError, match="cannot verify"):
        runners.install("ge", latest=True)


# ---------------------------------------------------------------------------
# H1: a network-controlled asset name must never become a path
# ---------------------------------------------------------------------------
def test_release_rejects_traversal_asset_name(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "tag_name": "v1",
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {
                "name": "GE-Proton../../../../home/user/.config/autostart/x-x86_64.tar.gz",
                "size": 1,
                "browser_download_url": "https://github.com/o/r",
            }
        ],
    }
    monkeypatch.setattr(runners.download, "fetch_json", lambda *a, **k: payload)
    with pytest.raises(runners.RunnerError):
        runners._release(runners._spec("ge"), None)


def test_release_accepts_a_plain_asset_name(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runners.download,
        "fetch_json",
        lambda *a, **k: _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z"),
    )
    rel = runners._release(runners._spec("ge"), None)
    assert rel["tar"]["name"] == "GE-Proton11-7-x86_64.tar.gz"


# ---------------------------------------------------------------------------
# L4: "latest"/"auto" must rank explicitly, not take the path-sorted last entry
# ---------------------------------------------------------------------------
def test_resolve_latest_prefers_the_newest_version(fake_runners_dir: Path) -> None:
    old = _make_runner(fake_runners_dir, "GE-Proton9-1", version="GE-Proton9-1")
    new = _make_runner(fake_runners_dir, "GE-Proton11-7", version="GE-Proton11-7")
    # Path sort would pick the 9-1 build; the version rank must pick 11-7.
    assert runners.resolve_runner("latest") == new
    assert runners.resolve_runner("auto") == new
    assert old != new


def test_resolve_latest_prefers_nadare_installed(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = _make_runner(fake_runners_dir, "Proton-Own", version="1")
    external = _make_runner(tmp_path / "steam", "Proton-Steam", version="999")
    monkeypatch.setattr(runners, "steam_runners", lambda: [external])
    assert runners.resolve_runner("latest") == own


# ---------------------------------------------------------------------------
# L6: the --latest trust anchor must come from a GitHub host
# ---------------------------------------------------------------------------
def test_published_sha512_rejects_untrusted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        runners.download, "http_get", lambda url, **k: called.append(url) or b"deadbeef x"
    )
    with pytest.raises(runners.RunnerError, match="untrusted host"):
        runners._published_sha512({"browser_download_url": "https://evil.example/x.sha512sum"})
    assert called == []


def test_published_sha512_accepts_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runners.download, "http_get", lambda url, **k: b"ABCDEF  file\n")
    digest = runners._published_sha512(
        {"browser_download_url": "https://github.com/o/r/releases/download/v1/x.sha512sum"}
    )
    assert digest == "abcdef"


def test_published_sha512_restricts_redirects_to_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NV-2: every redirect hop of the trust anchor must stay on GitHub."""
    captured: dict = {}

    def fake_http_get(url: str, **kwargs: object) -> bytes:
        captured.update(kwargs)
        return b"deadbeef x"

    monkeypatch.setattr(runners.download, "http_get", fake_http_get)
    runners._published_sha512(
        {"browser_download_url": "https://github.com/o/r/releases/download/v1/x.sha512sum"}
    )
    guard = captured["redirect_allowed"]
    assert guard("https://github.com/o/r/next") is True  # type: ignore[operator]
    assert guard("https://objects.githubusercontent.com/o/r/next") is True  # type: ignore[operator]
    assert guard("https://release-assets.githubusercontent.com/github-production-release-asset/1/2") is True  # type: ignore[operator]  # noqa: E501
    assert guard("https://evil.example/x") is False  # type: ignore[operator]
    assert guard("http://github.com/o/r/next") is False  # type: ignore[operator]


def test_install_rejects_offhost_tarball_url(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NV-2: the artifact host must be pinned as well as the checksum host."""
    payload = _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z", digest="a" * 64)
    payload["assets"][0]["browser_download_url"] = "https://evil.example/x.tar.gz"
    monkeypatch.setattr(runners.download, "fetch_json", lambda *a, **k: payload)

    def _must_not_download(*a: object, **k: object) -> None:
        raise AssertionError("must not download an off-host tarball")

    monkeypatch.setattr(runners.download, "download", _must_not_download)
    with pytest.raises(runners.RunnerError, match="untrusted host"):
        runners.install("ge", latest=True)


# ---------------------------------------------------------------------------
# L5: the release-lookup cache must be safe under concurrent callers
# ---------------------------------------------------------------------------
def test_latest_release_cache_is_thread_safe(
    fake_runners_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    def fake_release(spec, tag, **kwargs):
        time.sleep(0.02)
        return {"tag": "v1", "published_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(runners, "_release", fake_release)
    results: list[dict | None] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(runners._latest_release(runners._spec("ge")))
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(result is not None and result["tag"] == "v1" for result in results)
    cached = runners._LATEST_CACHE["GloriousEggroll/proton-ge-custom"][1]
    assert cached is not None and cached["tag"] == "v1"


# ---------------------------------------------------------------------------
# M4 / L7: extraction replaces an install safely and honours cancellation
# ---------------------------------------------------------------------------
def _make_tarball(tmp_path: Path, top_name: str) -> Path:
    src = tmp_path / f"src-{top_name}"
    (src / top_name).mkdir(parents=True)
    proton = src / top_name / "proton"
    proton.write_text("#!/bin/sh\n", encoding="utf-8")
    proton.chmod(0o755)
    tar_path = tmp_path / f"{top_name}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(src / top_name, arcname=top_name)
    return tar_path


def test_extract_replaces_existing_install_without_leaving_old(
    fake_runners_dir: Path, tmp_path: Path
) -> None:
    existing = _make_runner(fake_runners_dir, "GE-Proton11-7", version="old")
    (existing / "sentinel").write_text("old", encoding="utf-8")
    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    dest = runners._extract(tar, cancel=None, emit=runners.emitter(None))

    assert dest == existing
    assert (dest / "proton").is_file()
    assert not (dest / "sentinel").exists()
    assert not list(fake_runners_dir.glob(".GE-Proton11-7.old-*"))


def test_extract_restores_old_install_when_swap_fails(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = _make_runner(fake_runners_dir, "GE-Proton11-7", version="old")
    (existing / "sentinel").write_text("old", encoding="utf-8")
    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    real_replace = runners.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the swap of the new tree into place fails
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(runners.os, "replace", flaky)
    with pytest.raises(OSError):
        runners._extract(tar, cancel=None, emit=runners.emitter(None))

    assert existing.is_dir()
    assert (existing / "sentinel").read_text(encoding="utf-8") == "old"


def test_extract_checks_cancellation_between_members(
    fake_runners_dir: Path, tmp_path: Path
) -> None:
    from nadare.core.events import Cancelled

    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    class CancelOnSecond:
        def __init__(self) -> None:
            self.calls = 0

        def raise_if_cancelled(self) -> None:
            self.calls += 1
            if self.calls >= 2:  # first pre-loop check passes, first member cancels
                raise Cancelled()

    with pytest.raises(Cancelled):
        runners._extract(tar, cancel=CancelOnSecond(), emit=runners.emitter(None))

    assert not (fake_runners_dir / "GE-Proton11-7").exists()
    assert not list(fake_runners_dir.glob(".unpack-*"))


# ---------------------------------------------------------------------------
# NV-3: hidden crash leftovers must never be discovered or selected
# ---------------------------------------------------------------------------
def test_discovery_skips_hidden_stale_swap_trees(fake_runners_dir: Path) -> None:
    good = _make_runner(fake_runners_dir, "GE-Proton11-7", version="GE-Proton11-7")
    stale = _make_runner(
        fake_runners_dir, ".GE-Proton11-7.old-deadbeef", version="GE-Proton11-7"
    )

    assert runners.nadare_runners() == [good]
    assert stale not in runners.all_runners()
    assert runners.resolve_runner("latest") == good


def test_extract_prunes_stale_swap_leftovers(
    fake_runners_dir: Path, tmp_path: Path
) -> None:
    stale = _make_runner(fake_runners_dir, ".GE-Proton11-7.old-deadbeef")
    staging = fake_runners_dir / ".unpack-leftover"
    staging.mkdir()
    long_ago = time.time() - 7 * 3600
    os.utime(staging, (long_ago, long_ago))
    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    runners._extract(tar, cancel=None, emit=runners.emitter(None))

    assert not stale.exists()
    assert not staging.exists()


def test_published_sha512_empty_body_raises_runner_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runners.download, "http_get", lambda url, **k: b"  \n")
    with pytest.raises(runners.RunnerError):
        runners._published_sha512(
            {"browser_download_url": "https://github.com/o/r/releases/download/v1/x.sha512sum"}
        )


def test_runner_version_tolerates_non_utf8(fake_runners_dir: Path) -> None:
    runner = _make_runner(fake_runners_dir, "BadBytes")
    (runner / "version").write_bytes(b"\xff\xfe\x80")
    assert runners.runner_version(runner) == "BadBytes"


def test_install_checks_runners_dir_space_with_multiplier(
    fake_runners_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _release_json("GE-Proton11-7", "2026-01-01T00:00:00Z", digest="a" * 64)
    monkeypatch.setattr(runners.download, "fetch_json", lambda *a, **k: payload)

    def fake_download(url, dest, **kwargs):
        dest.write_bytes(b"x")
        return runners.download.DownloadResult(dest, 1, "b" * 64, "c" * 128)

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners, "_extract", lambda *a, **k: tmp_path / "runner")
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        runners.diskspace,
        "check_space",
        lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
    )
    runners.install("ge", latest=True)
    assert (paths.runners_dir(), 3 * 1024) in calls
