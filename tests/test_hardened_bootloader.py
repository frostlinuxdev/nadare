# SPDX-License-Identifier: GPL-3.0-or-later
"""Locked bootloader provenance and the AppImage hardening gate."""

import importlib.util
import io
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        "build_hardened_bootloader", ROOT / "scripts/build_hardened_bootloader.py"
    )
    module = importlib.util.module_from_spec(spec)
    # The helper imports its adjacent checker, like its command-line execution does.
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_pin_is_the_existing_lock_sdist():
    module = load()
    package = next(
        p
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if p["name"] == "pyinstaller"
    )
    assert module.locked_source(ROOT / "uv.lock") == (
        "6.22.3",
        package["sdist"]["url"],
        "05eb2f5615503e72939a7224d68b4aff572c6b0438ee4a17d0a4b481f399362d",
    )


def test_sdist_digest_checked_before_extract(tmp_path):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"untrusted archive")
    with pytest.raises(ValueError, match="SHA-256"):
        load().extract_verified(archive, "0" * 64, tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


def test_waf_runs_with_flags_and_checks_before_install(tmp_path, monkeypatch):
    module = load()
    source, installed = tmp_path / "source", tmp_path / "installed"
    (source / "bootloader").mkdir(parents=True)
    outputs = source / "PyInstaller/bootloader/Linux-64bit-intel"
    outputs.mkdir(parents=True)
    for name in ("run", "run_d"):
        (outputs / name).write_bytes(b"synthetic hardened bootloader")
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(
        module,
        "inspect_elf",
        lambda data: {
            "PIE": True,
            "full RELRO": True,
            "NX stack": True,
        },
    )
    module.compile_bootloaders(source, installed)
    args, kw = calls[0]
    assert args[1:] == ["./waf", "distclean", "all"]
    assert kw["cwd"] == source / "bootloader"
    assert kw["check"] is True
    assert kw["env"]["CFLAGS"] == "-fPIE"
    assert kw["env"]["LDFLAGS"] == "-pie -Wl,-z,relro,-z,now"
    # The release container has no zlib headers: PyInstaller builds its bundled zlib instead.
    assert kw["env"]["PYI_STATIC_ZLIB"] == "1"
    assert (installed / "bootloader/Linux-64bit-intel/run").read_bytes() == (
        outputs / "run"
    ).read_bytes()
    monkeypatch.setattr(module, "inspect_elf", lambda data: {"PIE": False})
    with pytest.raises(ValueError, match="hardening"):
        module.compile_bootloaders(source, installed)


def test_extraction_rejects_path_traversal(tmp_path):
    import hashlib

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises((ValueError, tarfile.FilterError)):
        load().extract_verified(
            archive, hashlib.sha256(archive.read_bytes()).hexdigest(), tmp_path / "unpacked"
        )
    assert not (tmp_path / "escape").exists()


def test_main_uses_locked_download_and_verified_extraction(tmp_path, monkeypatch):
    import hashlib

    module = load()
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("pyinstaller-6.22.3/bootloader/waf")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"fake"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(module, "locked_source", lambda lock: ("6.22.3", "locked-url", digest))
    installed = tmp_path / "installed/PyInstaller"
    monkeypatch.setitem(
        sys.modules,
        "PyInstaller",
        SimpleNamespace(
            __version__="6.22.3",
            __file__=str(installed / "__init__.py"),
        ),
    )
    downloads, builds = [], []

    def download(url, dest):
        downloads.append(url)
        dest.write_bytes(archive.read_bytes())

    def compile_fixture(source, target):
        assert (source / "bootloader/waf").read_bytes() == b"fake"
        builds.append(target)

    monkeypatch.delenv("NADARE_PYINSTALLER_SOURCE", raising=False)
    monkeypatch.setattr(module.urllib.request, "urlretrieve", download)
    monkeypatch.setattr(module, "compile_bootloaders", compile_fixture)
    module.main()
    assert downloads == ["locked-url"]
    assert builds == [installed]
    downloads.clear()
    monkeypatch.setenv("NADARE_PYINSTALLER_SOURCE", str(archive))
    module.main()
    assert downloads == []
    assert builds == [installed, installed]
    sys.modules["PyInstaller"].__version__ = "wrong"
    with pytest.raises(ValueError, match="match uv.lock"):
        module.main()
    assert len(builds) == 2


def test_appimage_rebuilds_before_freezing_and_checks_final_payload():
    script = (ROOT / "scripts/build_appimage.sh").read_text()
    rebuild = script.index('"$PY" scripts/build_hardened_bootloader.py')
    freeze = script.index('"$PY" -m PyInstaller')
    gate = script.index('"$PY" scripts/check_elf_hardening.py')
    wrap = script.index("ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1")
    assert rebuild < freeze < script.index("strip --strip-all") < gate < wrap
    assert "AppDir/usr/bin/nadare" in script[gate:wrap]
    assert '"$internal/nadare/assets/7zzs"' in script[gate:wrap]
