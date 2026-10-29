# SPDX-License-Identifier: GPL-3.0-or-later
"""Unpack the VC++ runtime DLLs from the redistributable instead of running its installer.

The installer can idle forever under Wine; the DLLs inside it are plain files. These tests build
a small Burn-like bundle (a UX cab, then a package cab holding a nested cab of ``*.dll_amd64``
files, as the real ``vc_redist.x64.exe`` does) and let the real 7-Zip unpack it.
"""

from __future__ import annotations

import subprocess

import pytest
from _cabfab import make_bundle, make_pe

from nadare.core import sevenzip, vcredist, vcunpack

pytestmark = pytest.mark.skipif(
    sevenzip.find() is None, reason="no 7-Zip available to unpack the test bundle"
)

NAMES = vcunpack.RUNTIME_DLLS


def _bundle(tmp_path, *, extra=None, drop=(), machine=0x8664):
    inner = {f"{n}_amd64": make_pe(machine) for n in NAMES if n not in drop}
    inner.update(extra or {})
    path = tmp_path / "vc_redist.x64.exe"
    path.write_bytes(make_bundle(inner))
    return path


def _prefix(tmp_path, *, placeholders=True):
    prefix = tmp_path / "pfx"
    system32 = prefix / "drive_c" / "windows" / "system32"
    system32.mkdir(parents=True)
    if placeholders:
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            (system32 / name).write_bytes(b"MZ" + b"Wine builtin DLL" + b"\0" * 100)
    return prefix, system32


def test_the_x64_runtime_lands_in_system32_and_counts_as_installed(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    assert vcredist.installed(prefix) is False
    copied = vcunpack.unpack(_bundle(tmp_path), prefix)
    assert sorted(copied) == sorted(NAMES)
    for name in NAMES:
        data = (system32 / name).read_bytes()
        assert data[:2] == b"MZ" and b"Wine builtin" not in data[:4096]
    assert vcredist.installed(prefix) is True


def test_arm64_and_other_files_in_the_bundle_are_ignored(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    bundle = _bundle(
        tmp_path,
        extra={
            "vcruntime140.dll_arm64": make_pe(0xAA64, 9000),
            "mfc140.dll_amd64": make_pe(),
            "readme.txt": b"hello",
        },
    )
    vcunpack.unpack(bundle, prefix)
    assert (system32 / "vcruntime140.dll").stat().st_size == 6000, "the arm64 build must not win"
    assert not (system32 / "mfc140.dll").exists()
    assert not (system32 / "readme.txt").exists()


def test_a_dll_for_another_cpu_under_the_x64_name_is_refused(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    before = (system32 / "vcruntime140.dll").read_bytes()
    with pytest.raises(vcunpack.UnpackError, match="x64"):
        vcunpack.unpack(_bundle(tmp_path, machine=0xAA64), prefix)
    assert (system32 / "vcruntime140.dll").read_bytes() == before, "nothing is half-copied"


def test_a_file_that_is_not_a_pe_is_refused(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    bundle = _bundle(tmp_path, extra={"msvcp140.dll_amd64": b"not a dll at all" * 500})
    with pytest.raises(vcunpack.UnpackError):
        vcunpack.unpack(bundle, prefix)
    assert vcredist.installed(prefix) is False


def test_a_bundle_without_the_core_dlls_is_refused(tmp_path):
    prefix, _ = _prefix(tmp_path)
    with pytest.raises(vcunpack.UnpackError, match="vcruntime140"):
        vcunpack.unpack(_bundle(tmp_path, drop=("vcruntime140.dll",)), prefix)


def test_optional_dlls_may_be_missing(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    copied = vcunpack.unpack(_bundle(tmp_path, drop=("vcamp140.dll", "vcomp140.dll")), prefix)
    assert "vcamp140.dll" not in copied
    assert (system32 / "msvcp140.dll").is_file()


def test_a_real_dll_already_there_is_kept_and_a_placeholder_or_gap_is_filled(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    (system32 / "msvcp140.dll").write_bytes(b"MZ" + b"newer real runtime" * 400)
    kept = (system32 / "msvcp140.dll").read_bytes()
    vcunpack.unpack(_bundle(tmp_path), prefix)
    assert (system32 / "msvcp140.dll").read_bytes() == kept
    assert (system32 / "vcruntime140.dll").read_bytes()[:2] == b"MZ"
    assert b"Wine builtin" not in (system32 / "vcruntime140.dll").read_bytes()[:4096]
    assert (system32 / "concrt140.dll").is_file()


def test_a_planted_symlink_is_replaced_not_followed(tmp_path):
    prefix, system32 = _prefix(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (system32 / "vcruntime140.dll").unlink()
    (system32 / "vcruntime140.dll").symlink_to(victim)
    vcunpack.unpack(_bundle(tmp_path), prefix)
    assert victim.read_text() == "precious"
    assert not (system32 / "vcruntime140.dll").is_symlink()


def test_no_prefix_means_no_unpack(tmp_path):
    with pytest.raises(vcunpack.UnpackError, match="prefix"):
        vcunpack.unpack(_bundle(tmp_path), tmp_path / "missing")


def test_without_7zip_it_says_so(tmp_path, monkeypatch):
    prefix, _ = _prefix(tmp_path)
    monkeypatch.setattr(vcunpack.sevenzip, "find", lambda: None)
    with pytest.raises(vcunpack.UnpackError, match="7-Zip"):
        vcunpack.unpack(_bundle(tmp_path), prefix)


def test_a_stuck_7zip_is_given_up_on(tmp_path):
    prefix, _ = _prefix(tmp_path)

    def stuck(*_a, **_k):
        raise subprocess.TimeoutExpired("7z", 1)

    with pytest.raises(vcunpack.UnpackError, match="too long"):
        vcunpack.unpack(_bundle(tmp_path), prefix, run=stuck)


def test_the_scratch_folder_is_removed(tmp_path, monkeypatch):
    prefix, _ = _prefix(tmp_path)
    made: list = []
    real = vcunpack.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(vcunpack.tempfile, "mkdtemp", spy)
    vcunpack.unpack(_bundle(tmp_path), prefix)
    assert made
    import os

    assert not any(os.path.exists(d) for d in made)
