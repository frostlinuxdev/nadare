# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.nte_launcher (no network or 7z needed)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from nadare.core import nte_launcher as nl

MZ = b"MZ" + b"\0" * 64
INSTALLER_BYTES = b"nsis"


@pytest.fixture(autouse=True)
def _pin_the_fake_installer(monkeypatch):
    monkeypatch.setattr(nl, "INSTALLER_SHA256", hashlib.sha256(INSTALLER_BYTES).hexdigest())


def _fake_7z(calls, *, inner=True, launcher=True):
    """Pretend 7z: the NSIS installer holds WmGpLaunchSetupFiles.7z, which holds NTEGlobal/."""

    def run(argv, **kw):
        calls.append(argv)
        outdir = Path(next(a for a in argv if a.startswith("-o"))[2:])
        archive = Path(argv[-1])  # [7z, "x", "-y", "-o<dir>", <archive>]
        if archive.suffix == ".exe":
            if inner:
                (outdir / "$PLUGINSDIR").mkdir(parents=True)
                (outdir / "WmGpLaunchSetupFiles.7z").write_bytes(b"inner-7z")
        else:
            root = outdir / "NTEGlobal"
            (root / "Config").mkdir(parents=True)
            (root / "Config" / "a.ini").write_text("x")
            if launcher:
                (root / "NTEGlobalLauncher.exe").write_bytes(MZ)
            (outdir / "NTEGlobalLauncher.exe").write_bytes(
                MZ
            )  # the bootstrap copy in the payload root
            (outdir / "uninst.exe").write_bytes(MZ)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _dl(urls, data=INSTALLER_BYTES):
    def download(url, dest):
        urls.append(url)
        Path(dest).write_bytes(data)

    return download


def test_launcher_path():
    assert nl.launcher_path(Path("/g/NTE")) == Path("/g/NTE/NTEGlobal/NTEGlobalLauncher.exe")


def test_nte_backend_reuses_the_core_installer_url():
    from nadare.backends import launcher

    assert launcher.NteBackend.default_installer_url == nl.INSTALLER_URL


def test_installed_launcher_is_left_alone(tmp_path):
    root = tmp_path / "NTE"
    exe = nl.launcher_path(root)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(MZ)
    got = nl.ensure_launcher(
        root,
        data_dir=tmp_path / "d",
        download=lambda *a: 1 / 0,
        which=lambda c: 1 / 0,
        run=lambda *a, **k: 1 / 0,
    )
    assert got == exe


def test_ensure_launcher_downloads_unpacks_and_copies_ntegloba(tmp_path):
    root = tmp_path / "NTE"
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "keep.txt").write_text("game")
    urls: list = []
    calls: list = []
    which = lambda c: "/usr/bin/7z" if c == "7z" else None  # noqa: E731
    got = nl.ensure_launcher(
        root, data_dir=tmp_path / "d", download=_dl(urls), which=which, run=_fake_7z(calls)
    )
    assert got == root / "NTEGlobal" / "NTEGlobalLauncher.exe" and got.read_bytes() == MZ
    assert (root / "NTEGlobal" / "Config" / "a.ini").read_text() == "x"
    assert (root / "Client" / "keep.txt").read_text() == "game"  # the game files are untouched
    assert urls == [nl.INSTALLER_URL] and len(calls) == 2
    assert calls[0][0] == "/usr/bin/7z" and calls[0][1] == "x"
    assert not (
        root / "uninst.exe"
    ).exists()  # only NTEGlobal/ is taken, not the installer's extras


def test_installer_is_cached_between_runs(tmp_path):
    urls: list = []
    which = lambda c: "/usr/bin/7z"  # noqa: E731
    for name in ("A", "B"):
        nl.ensure_launcher(
            tmp_path / name,
            data_dir=tmp_path / "d",
            download=_dl(urls),
            which=which,
            run=_fake_7z([]),
        )
    assert urls == [nl.INSTALLER_URL]  # downloaded once


def test_missing_7z_and_broken_payloads_are_explained(tmp_path):
    with pytest.raises(nl.NteLauncherError, match="7z"):
        nl.ensure_launcher(
            tmp_path / "a",
            data_dir=tmp_path / "d1",
            download=_dl([]),
            which=lambda c: None,
            run=_fake_7z([]),
        )
    with pytest.raises(nl.NteLauncherError, match="WmGpLaunchSetupFiles"):
        nl.ensure_launcher(
            tmp_path / "b",
            data_dir=tmp_path / "d2",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], inner=False),
        )
    with pytest.raises(nl.NteLauncherError, match="NTEGlobalLauncher.exe"):
        nl.ensure_launcher(
            tmp_path / "c",
            data_dir=tmp_path / "d3",
            download=_dl([]),
            which=lambda c: "/x/7z",
            run=_fake_7z([], launcher=False),
        )
    assert not (tmp_path / "c" / "NTEGlobal").exists()  # nothing half-installed


def test_fetch_installer_verifies_and_caches_good_bytes(tmp_path, monkeypatch):
    data = b"good installer"
    monkeypatch.setattr(nl, "INSTALLER_SHA256", hashlib.sha256(data).hexdigest())
    cache = tmp_path / "d" / "NTE_setup.exe"
    urls: list = []

    def download(url, dest):
        urls.append(url)
        Path(dest).write_bytes(data)

    assert nl._fetch_installer(cache, download) == cache
    assert cache.read_bytes() == data and urls == [nl.INSTALLER_URL]
    # A second call reuses the verified cache without downloading again.
    assert nl._fetch_installer(cache, lambda *a: pytest.fail("downloaded again")) == cache


def test_fetch_installer_rejects_wrong_bytes_and_leaves_no_cache(tmp_path, monkeypatch):
    expected = b"expected installer"
    monkeypatch.setattr(nl, "INSTALLER_SHA256", hashlib.sha256(expected).hexdigest())
    cache = tmp_path / "d" / "NTE_setup.exe"

    with pytest.raises(nl.NteLauncherError, match="expected .*got"):
        nl._fetch_installer(cache, _dl([], data=b"tampered"))
    assert not cache.exists()
    assert list(cache.parent.iterdir()) == []


def test_fetch_installer_replaces_a_poisoned_cache(tmp_path, monkeypatch):
    good = b"good installer"
    monkeypatch.setattr(nl, "INSTALLER_SHA256", hashlib.sha256(good).hexdigest())
    cache = tmp_path / "d" / "NTE_setup.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"poisoned")
    urls: list = []

    assert nl._fetch_installer(cache, _dl(urls, data=good)) == cache
    assert cache.read_bytes() == good and urls == [nl.INSTALLER_URL]
