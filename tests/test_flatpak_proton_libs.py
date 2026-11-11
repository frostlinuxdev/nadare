# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: the libraries Proton's own files need that the Flatpak runtime lacks.

Proton is built for the Steam Runtime. Inside the Flatpak it runs on the Freedesktop runtime,
which has newer, incompatible versions (``libvpx.so.11``, ``libtheoradec.so.2``) and no Kerberos.
Without ``libvpx.so.9`` and ``libbz2.so.1.0`` Proton's FFmpeg does not load, Wine cannot decode
video, and a game that plays an intro video quits (Girls' Frontline 2 exited with code 5).
The Flatpak ships pinned Debian copies in a folder of their own, and only Proton is pointed at
it, so Nadare itself keeps using the runtime's libraries.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from nadare.core import sandbox

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "flatpak" / "stage_repo.py"
PINS = REPO / "packaging" / "flatpak" / "proton-libs.json"

NEEDED = {
    "libvpx.so.9",
    "libbz2.so.1.0",
    "libtheoradec.so.1",
    "libtheoraenc.so.1",
    "libgssapi_krb5.so.2",
}


@pytest.fixture(scope="module")
def stage():
    pytest.importorskip("yaml")
    spec = importlib.util.spec_from_file_location("stage_repo_proton_libs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the pin list --------------------------------------------------------------------------------
def test_every_package_is_pinned_by_sha256_for_both_architectures():
    entries = json.loads(PINS.read_text())
    assert entries
    for entry in entries:
        assert entry["url"].startswith("https://deb.debian.org/debian/pool/main/"), entry
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0, entry
        assert entry["arch"] in {"amd64", "i386"}, entry
    names = {(e["url"].rsplit("/", 1)[1].split("_")[0], e["arch"]) for e in entries}
    for package in {n for n, _a in names}:
        assert (package, "amd64") in names and (package, "i386") in names, package


# ---- unpacking a package -------------------------------------------------------------------------
def _ar(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members.items():
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        out.write(header.encode())
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def _fake_deb(files: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as tar:
        for path, content in files.items():
            info = tarfile.TarInfo("./" + path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for path, target in (symlinks or {}).items():
            info = tarfile.TarInfo("./" + path)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return _ar(
        {"debian-binary": b"2.0\n", "control.tar.xz": b"", "data.tar.xz": data.getvalue()}
    )


def test_libraries_and_copyright_files_are_extracted(stage, tmp_path):
    deb = tmp_path / "libvpx9_1.15.0_amd64.deb"
    deb.write_bytes(
        _fake_deb(
            {
                "usr/lib/x86_64-linux-gnu/libvpx.so.9.1.0": b"\x7fELF fake",
                "usr/share/doc/libvpx9/copyright": b"BSD",
                "usr/bin/vpxenc": b"not wanted",
            },
            {"usr/lib/x86_64-linux-gnu/libvpx.so.9": "libvpx.so.9.1.0"},
        )
    )
    libdir = tmp_path / "out" / "x86_64"
    licences = tmp_path / "out" / "licences"
    stage.extract_deb(deb, libdir, licences)
    assert (libdir / "libvpx.so.9.1.0").read_bytes() == b"\x7fELF fake"
    assert (libdir / "libvpx.so.9").is_symlink()
    assert (libdir / "libvpx.so.9").resolve() == (libdir / "libvpx.so.9.1.0").resolve()
    assert not (libdir / "vpxenc").exists()
    assert (licences / "libvpx9" / "copyright").read_bytes() == b"BSD"


def test_a_symlink_pointing_outside_is_refused(stage, tmp_path):
    deb = tmp_path / "evil_1_amd64.deb"
    deb.write_bytes(_fake_deb({}, {"usr/lib/x86_64-linux-gnu/libx.so.1": "../../../../etc/passwd"}))
    with pytest.raises((ValueError, SystemExit)):
        stage.extract_deb(deb, tmp_path / "out" / "x86_64", tmp_path / "out" / "licences")


def test_a_download_with_the_wrong_checksum_is_refused(stage, tmp_path, monkeypatch):
    payload = _fake_deb({"usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4": b"x"})
    monkeypatch.setattr(stage, "_download", lambda url, target: target.write_bytes(payload))
    entry = {"url": "https://deb.debian.org/debian/pool/main/b/bzip2/libbz2-1.0_1_amd64.deb",
             "sha256": "0" * 64, "arch": "amd64"}
    with pytest.raises((ValueError, SystemExit)):
        stage.fetch_deb(entry, tmp_path / "cache")


def test_a_cached_package_with_the_right_checksum_is_not_downloaded_again(stage, tmp_path,
                                                                          monkeypatch):
    payload = _fake_deb({"usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4": b"x"})
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "libbz2-1.0_1_amd64.deb").write_bytes(payload)
    monkeypatch.setattr(stage, "_download", lambda url, target: pytest.fail("downloaded"))
    entry = {"url": "https://deb.debian.org/debian/pool/main/b/bzip2/libbz2-1.0_1_amd64.deb",
             "sha256": hashlib.sha256(payload).hexdigest(), "arch": "amd64"}
    assert stage.fetch_deb(entry, cache) == cache / "libbz2-1.0_1_amd64.deb"


# ---- Proton is pointed at them -----------------------------------------------------------------
def test_proton_direct_puts_the_shipped_libraries_on_the_library_path(tmp_path, monkeypatch):
    libs = tmp_path / "app" / "lib" / "nadare-proton"
    (libs / "x86_64").mkdir(parents=True)
    (libs / "i386").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "PROTON_LIBS", libs)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/already/there")
    _argv, env = sandbox.proton_direct(tmp_path / "runner", tmp_path / "prefix", ["game.exe"])
    parts = env["LD_LIBRARY_PATH"].split(":")
    assert parts[:2] == [str(libs / "x86_64"), str(libs / "i386")]
    assert "/already/there" in parts


def test_without_the_folder_nothing_is_added(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "PROTON_LIBS", tmp_path / "missing")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    _argv, env = sandbox.proton_direct(tmp_path / "runner", tmp_path / "prefix", ["game.exe"])
    assert "LD_LIBRARY_PATH" not in env


def test_the_list_covers_what_proton_was_seen_missing():
    """Measured with ldd inside the Flatpak against dwproton 11.0-15 (2026-11-09)."""
    entries = json.loads(PINS.read_text())
    packages = {e["url"].rsplit("/", 1)[1].split("_")[0] for e in entries}
    provides = {
        "libvpx9": {"libvpx.so.9"},
        "libbz2-1.0": {"libbz2.so.1.0"},
        "libtheora0": {"libtheoradec.so.1", "libtheoraenc.so.1"},
        "libgssapi-krb5-2": {"libgssapi_krb5.so.2"},
    }
    covered = set().union(*(provides.get(p, set()) for p in packages))
    assert NEEDED <= covered, NEEDED - covered
