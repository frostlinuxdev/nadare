# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.backends.czn.runners (fake network only)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from nadare.backends.czn import runners as czn_runners

PINNED_GE = czn_runners.RUNNERS["ge"]["tested"][1]


def _release_payload(*assets: dict) -> dict:
    return {"tag_name": "v1", "assets": list(assets)}


def _asset(name: str, url: str, size: int = 1024) -> dict:
    return {"name": name, "size": size, "browser_download_url": url}


def _serve_release(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(
        czn_runners, "_get", lambda url, timeout=30: io.BytesIO(json.dumps(payload).encode())
    )


def _make_tarball(tmp_path: Path, top: str) -> Path:
    src = tmp_path / f"src-{top}"
    (src / top).mkdir(parents=True)
    (src / top / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    tar_path = tmp_path / f"{top}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(src / top, arcname=top)
    return tar_path


# ---------------------------------------------------------------------------
# F1: a network-controlled asset name must never become a path
# ---------------------------------------------------------------------------
def test_release_rejects_traversal_asset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_release(
        monkeypatch,
        _release_payload(
            _asset(
                "GE-Proton../../../../home/user/.config/autostart/x-x86_64.tar.gz",
                "https://github.com/o/r",
            )
        ),
    )
    with pytest.raises(czn_runners.Fail, match="unsafe runner asset name"):
        czn_runners.release("ge", None)


def test_release_accepts_a_plain_asset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_release(
        monkeypatch,
        _release_payload(
            _asset("GE-Proton11-7-x86_64.tar.gz", "https://github.com/o/r/x"),
        ),
    )
    rel = czn_runners.release("ge", None)
    assert rel["tar"]["name"] == "GE-Proton11-7-x86_64.tar.gz"


# ---------------------------------------------------------------------------
# F2: the tarball and its .sha512sum must come from pinned GitHub hosts
# ---------------------------------------------------------------------------
def test_download_rejects_offhost_asset_before_fetching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(
        czn_runners.download, "http_open", lambda *a, **k: opened.append(a) or None
    )
    with pytest.raises(czn_runners.Fail, match="untrusted host"):
        czn_runners._download(
            _asset("x.tar.gz", "https://evil.example/x.tar.gz"), tmp_path / "part"
        )
    assert opened == []


def test_download_pins_redirects_to_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}

    class _Resp:
        def read(self, amount: int = -1) -> bytes:
            return b""

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_open(url: str, **kwargs: object) -> _Resp:
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(czn_runners.download, "http_open", fake_open)
    czn_runners._download(
        _asset("x.tar.gz", "https://github.com/o/r/x.tar.gz", size=0), tmp_path / "part"
    )
    guard = captured["redirect_allowed"]
    assert guard("https://github.com/o/r/next") is True  # type: ignore[operator]
    assert guard("https://objects.githubusercontent.com/o/r/next") is True  # type: ignore[operator]
    assert guard("https://release-assets.githubusercontent.com/github-production-release-asset/1/2") is True  # type: ignore[operator]  # noqa: E501
    assert guard("https://evil.example/x") is False  # type: ignore[operator]
    assert guard("http://github.com/o/r/next") is False  # type: ignore[operator]


def test_install_refuses_offhost_sha512sum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve_release(
        monkeypatch,
        _release_payload(
            _asset("GE-Proton11-7-x86_64.tar.gz", "https://github.com/o/r/x"),
            _asset("GE-Proton11-7-x86_64.sha512sum", "https://evil.example/x.sha512sum"),
        ),
    )

    def _must_not_download(*a: object, **k: object) -> None:
        raise AssertionError("must not download from an untrusted sum host")

    monkeypatch.setattr(czn_runners, "_download", _must_not_download)
    monkeypatch.setattr(czn_runners, "CACHE_DIR", tmp_path / "cache")
    with pytest.raises(czn_runners.Fail, match="untrusted host"):
        czn_runners.install("ge", latest=True)


# ---------------------------------------------------------------------------
# F3: a large runner download gets a disk-space check first
# ---------------------------------------------------------------------------
def _pinned_release(size: int) -> dict:
    return {
        "tag": "GE-Proton11-7",
        "tar": _asset("GE-Proton11-7-x86_64.tar.gz", "https://github.com/o/r/x", size=size),
        "sum": None,
    }


def test_install_checks_disk_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(czn_runners, "release", lambda rid, tag: _pinned_release(4096))
    monkeypatch.setattr(czn_runners, "_download", lambda asset, dest: (PINNED_GE, "0" * 128))
    monkeypatch.setattr(czn_runners, "_extract", lambda path: tmp_path / "runner")
    monkeypatch.setattr(czn_runners, "CACHE_DIR", tmp_path / "cache")
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        czn_runners.diskspace,
        "check_space",
        lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
    )
    czn_runners.install("ge", latest=False)
    assert calls and calls[0][1] == 4096


def test_install_reports_a_full_disk_as_a_network_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nadare.core.backends import BackendError

    monkeypatch.setattr(czn_runners, "release", lambda rid, tag: _pinned_release(4096))
    monkeypatch.setattr(czn_runners, "CACHE_DIR", tmp_path / "cache")

    def _no_space(path: object, needed: int, margin: float = 0.05) -> None:
        raise BackendError("Not enough disk space", code=4)

    monkeypatch.setattr(czn_runners.diskspace, "check_space", _no_space)
    with pytest.raises(czn_runners.Fail) as excinfo:
        czn_runners.install("ge", latest=False)
    assert excinfo.value.code == 4


# ---------------------------------------------------------------------------
# F4: replacing an install must not delete the working tree before the swap
# ---------------------------------------------------------------------------
def test_extract_restores_old_install_when_swap_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runners"
    monkeypatch.setattr(czn_runners, "RUNNERS_DIR", root)
    existing = root / "GE-Proton11-7"
    existing.mkdir(parents=True)
    (existing / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (existing / "sentinel").write_text("old", encoding="utf-8")
    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    real_replace = czn_runners.os.replace
    calls = {"n": 0}

    def flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # the swap of the new tree into place fails
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(czn_runners.os, "replace", flaky)
    with pytest.raises(OSError):
        czn_runners._extract(tar)

    assert (existing / "sentinel").read_text(encoding="utf-8") == "old"


def test_extract_replaces_existing_install_without_leftovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runners"
    monkeypatch.setattr(czn_runners, "RUNNERS_DIR", root)
    existing = root / "GE-Proton11-7"
    existing.mkdir(parents=True)
    (existing / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (existing / "sentinel").write_text("old", encoding="utf-8")
    tar = _make_tarball(tmp_path, "GE-Proton11-7")

    dest = czn_runners._extract(tar)

    assert dest == existing
    assert not (dest / "sentinel").exists()
    assert not list(root.glob(".GE-Proton11-7.old-*"))


# ---------------------------------------------------------------------------
# F10: concurrent installs must not share scratch paths
# ---------------------------------------------------------------------------
def test_install_uses_unique_part_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(czn_runners, "release", lambda rid, tag: _pinned_release(0))
    monkeypatch.setattr(czn_runners, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(czn_runners, "_extract", lambda path: tmp_path / "runner")
    seen: list[Path] = []

    def fake_download(asset: dict, dest: Path) -> tuple[str, str]:
        seen.append(dest)
        return PINNED_GE, "0" * 128

    monkeypatch.setattr(czn_runners, "_download", fake_download)
    czn_runners.install("ge", latest=False)
    czn_runners.install("ge", latest=False)
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_extract_uses_a_unique_scratch_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runners"
    monkeypatch.setattr(czn_runners, "RUNNERS_DIR", root)
    tar = _make_tarball(tmp_path, "Proton-Test")
    created: list[str] = []
    real_mkdtemp = czn_runners.tempfile.mkdtemp

    def spy(*a: object, **k: object) -> str:
        directory = real_mkdtemp(*a, **k)
        created.append(directory)
        return directory

    monkeypatch.setattr(czn_runners.tempfile, "mkdtemp", spy)
    czn_runners._extract(tar)
    assert len(created) == 1
    assert Path(created[0]).name.startswith(".unpack-")
