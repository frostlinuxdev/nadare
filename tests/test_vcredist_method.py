# SPDX-License-Identifier: GPL-3.0-or-later
"""Unpack first, install second: the VC++ runtime without the installer that can hang.

``vcredist_method`` (profile) or ``NADARE_VCREDIST_METHOD`` (environment) picks ``unpack`` (the
default) or ``installer`` (the old order, unpack only as a fallback). After unpacking, the
registry keys the real installer writes are imported so anything that checks them agrees.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from _cabfab import make_bundle, make_pe

from nadare.core import sevenzip, vcredist, vcunpack
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

pytestmark = pytest.mark.skipif(
    sevenzip.find() is None, reason="no 7-Zip available to unpack the test bundle"
)

VERSION = (14, 44, 35211, 0)


@pytest.fixture(autouse=True)
def _no_method_env(monkeypatch):
    monkeypatch.delenv("NADARE_VCREDIST_METHOD", raising=False)
    monkeypatch.delenv("NADARE_VCREDIST_TIMEOUT", raising=False)


def _bundle_bytes() -> bytes:
    inner = {f"{n}_amd64": make_pe(version=VERSION) for n in vcunpack.RUNTIME_DLLS}
    return make_bundle(inner)


class Rig:
    """A prefix, a cache holding the (fake) redistributable, and a recording runner."""

    def __init__(self, tmp_path, *, profile=None, payload=None, regedit_code=0):
        self.payload = payload if payload is not None else _bundle_bytes()
        self.prefix = tmp_path / "pfx"
        self.system32 = self.prefix / "drive_c" / "windows" / "system32"
        self.system32.mkdir(parents=True)
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            (self.system32 / name).write_bytes(b"MZ" + b"Wine builtin DLL" + b"\0" * 64)
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        (self.cache / "vc_redist.x64.exe").write_bytes(self.payload)
        self.profile = profile or {}
        self.calls: list[list[str]] = []
        self.reg_text: list[str] = []
        self.events: list = []
        self.regedit_code = regedit_code

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "regedit":
            path = argv[2]
            assert path.startswith("Z:\\"), path
            self.reg_text.append(Path(path[2:].replace("\\", "/")).read_text(encoding="utf-8"))
            if isinstance(self.regedit_code, BaseException):
                raise self.regedit_code
            return self.regedit_code
        return 0

    def install(self, cancel=None):
        vcredist.install(
            self.prefix,
            profile=self.profile,
            cache_dir=self.cache,
            run_in_prefix=self.run,
            emit=self.events.append,
            cancel=cancel or Cancel(),
            constant=hashlib.sha256(self.payload).hexdigest(),
        )

    @property
    def installer_ran(self):
        return any(c[0].endswith("vc_redist.x64.exe") for c in self.calls)

    @property
    def regedits(self):
        return [c for c in self.calls if c[0] == "regedit"]


# ---- the method switch ------------------------------------------------------------------
def test_the_default_is_to_unpack_and_never_start_the_installer(tmp_path):
    rig = Rig(tmp_path)
    rig.install()
    assert not rig.installer_ran
    assert vcredist.installed(rig.prefix) is True
    assert (rig.system32 / "vcruntime140.dll").stat().st_size == 6000


def test_the_profile_can_ask_for_the_installer_first(tmp_path):
    rig = Rig(tmp_path, profile={"vcredist_method": "installer"})
    rig.install()
    assert rig.installer_ran
    assert (rig.system32 / "vcruntime140.dll").read_bytes()[2:18] == b"Wine builtin DLL"


def test_the_environment_beats_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("NADARE_VCREDIST_METHOD", "installer")
    rig = Rig(tmp_path, profile={"vcredist_method": "unpack"})
    rig.install()
    assert rig.installer_ran
    monkeypatch.setenv("NADARE_VCREDIST_METHOD", "unpack")
    rig2 = Rig(tmp_path / "again", profile={"vcredist_method": "installer"})
    rig2.install()
    assert not rig2.installer_ran


def test_an_unknown_method_means_the_default(tmp_path):
    rig = Rig(tmp_path, profile={"vcredist_method": "magic"})
    rig.install()
    assert not rig.installer_ran


def test_the_method_may_sit_in_the_profile_extra(tmp_path):
    rig = Rig(tmp_path, profile={"extra": {"vcredist_method": "installer"}})
    rig.install()
    assert rig.installer_ran


def test_a_bundle_that_cannot_be_unpacked_falls_back_to_the_installer(tmp_path):
    rig = Rig(tmp_path, payload=b"MZ-not-a-real-redistributable")
    rig.install()
    assert rig.installer_ran
    assert not any("unpack" in e.message.lower() for e in rig.events if e.kind == "status")


def test_a_good_unpack_says_so_once_and_offers_no_skip(tmp_path):
    rig = Rig(tmp_path)
    rig.install()
    statuses = [e.message for e in rig.events if e.kind == "status"]
    assert len(statuses) == 1 and "unpack" in statuses[0].lower()
    assert not any((e.data or {}).get("skippable") for e in rig.events)


# ---- the registry keys ------------------------------------------------------------------
def test_the_runtime_keys_are_imported_with_the_dlls_own_version(tmp_path):
    rig = Rig(tmp_path)
    rig.install()
    assert len(rig.regedits) == 1
    assert rig.regedits[0][1] == "/S"
    text = rig.reg_text[0]
    for root in ("SOFTWARE", "SOFTWARE\\Wow6432Node"):
        key = f"[HKEY_LOCAL_MACHINE\\{root}\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64]"
        assert key in text
    assert text.count('"Installed"=dword:00000001') == 2
    assert text.count('"Bld"=dword:0000898b') == 2  # 35211
    assert text.count('"Minor"=dword:0000002c') == 2  # 44
    assert text.count('"Major"=dword:0000000e') == 2
    assert text.count('"Rbld"=dword:00000000') == 2
    assert text.count('"Version"="v14.44.35211.00"') == 2
    assert text.startswith("Windows Registry Editor Version 5.00")


def test_a_runtime_without_a_version_resource_writes_no_keys(tmp_path):
    inner = {f"{n}_amd64": make_pe() for n in vcunpack.RUNTIME_DLLS}
    rig = Rig(tmp_path, payload=make_bundle(inner))
    rig.install()
    assert rig.regedits == []
    assert vcredist.installed(rig.prefix) is True


@pytest.mark.parametrize("outcome", [3, BackendError("regedit failed"), OSError("no runner")])
def test_a_registry_failure_never_fails_the_install(tmp_path, outcome):
    rig = Rig(tmp_path, regedit_code=outcome)
    rig.install()  # must not raise
    assert vcredist.installed(rig.prefix) is True


def test_a_cancel_during_the_registry_import_is_honoured(tmp_path):
    rig = Rig(tmp_path, regedit_code=Cancelled())
    with pytest.raises(Cancelled):
        rig.install()


def test_a_stuck_registry_import_is_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(vcredist, "REGISTRY_IMPORT_TIMEOUT", 0.3)
    release = threading.Event()
    rig = Rig(tmp_path)
    real_run = rig.run

    def run(argv):
        if argv[0] == "regedit":
            release.wait(5)
            return 0
        return real_run(argv)

    rig.run = run
    try:
        rig.install()  # returns after the short limit instead of waiting for regedit
        assert vcredist.installed(rig.prefix) is True
    finally:
        release.set()


def test_the_fallback_after_a_hung_installer_imports_the_keys_too(tmp_path, monkeypatch):
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "0.3")
    release = threading.Event()
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: release.set() or 1)
    rig = Rig(tmp_path, profile={"vcredist_method": "installer"})
    real_run = rig.run

    def run(argv):
        if argv[0].endswith("vc_redist.x64.exe"):
            rig.calls.append(list(argv))
            release.wait(10)
            return 1
        return real_run(argv)

    rig.run = run
    rig.install()
    assert len(rig.regedits) == 1
    assert vcredist.installed(rig.prefix) is True


# ---- the helpers ------------------------------------------------------------------------
def test_runtime_version_reads_the_file_version(tmp_path):
    rig = Rig(tmp_path)
    vcunpack.unpack(Path(rig.cache / "vc_redist.x64.exe"), rig.prefix)
    assert vcunpack.runtime_version(rig.prefix) == VERSION


def test_runtime_version_is_none_when_it_cannot_be_read(tmp_path):
    prefix = tmp_path / "p"
    (prefix / "drive_c" / "windows" / "system32").mkdir(parents=True)
    assert vcunpack.runtime_version(prefix) is None
    (prefix / "drive_c/windows/system32/vcruntime140.dll").write_bytes(b"MZ" + b"\0" * 5000)
    assert vcunpack.runtime_version(prefix) is None


def test_registry_text_formats_a_version():
    text = vcunpack.registry_text((14, 44, 35211, 0))
    assert '"Version"="v14.44.35211.00"' in text
    assert vcunpack.registry_text((14, 38, 33135, 7)).count('"Version"="v14.38.33135.07"') == 2
