# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for audit findings C-06, C-08, C-09 and C-10."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.backends.fgo.lepton import LeptonError, LeptonFgo
from nadare.backends.script import ScriptBackend
from nadare.backends.wine import WineBackend
from nadare.core.backends import BackendError


# ---- C-06 -------------------------------------------------------------------
def test_batch_script_resolves_the_same_base_for_status_and_launch(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "script", {"launch_exe": "run.bat"})
    script = ctx.data_dir() / "run.bat"
    script.write_text("@echo off\n")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert ScriptBackend(ctx)._script() == script
    assert WineBackend(ctx)._target() == script


# ---- C-08 -------------------------------------------------------------------
def test_uninstall_refuses_a_prefix_another_profile_shares(tmp_path):
    prefix = tmp_path / "shared-prefix"
    write_exe(prefix / "drive_c" / "x.exe", "MZ")
    ctx = make_context(tmp_path, ak.KIND, {"prefix": str(prefix), "installed_prefix": str(prefix)})
    ctx.config.profiles["other"] = {"backend": ak.KIND, "prefix": str(prefix)}

    with pytest.raises(BackendError, match="shared with"):
        ArknightsBackend(ctx).uninstall()

    assert (prefix / "drive_c" / "x.exe").is_file()


# ---- C-09 -------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["--all", "-all", "-x", "a b", "", "../x"])
def test_lepton_context_cannot_be_an_option(bad):
    if not bad:
        assert LeptonFgo(context=bad).context == "fgo"
        return
    with pytest.raises(LeptonError):
        LeptonFgo(context=bad)


def test_lepton_per_call_context_and_release_are_checked():
    adapter = LeptonFgo(context="fgo")
    with pytest.raises(LeptonError):
        adapter.cmd_uninstall("--all")
    with pytest.raises(LeptonError):
        adapter.cmd_verify("fgo", "--all")
    assert adapter.cmd_uninstall("other")[2] == "other"


def test_fgo_backend_turns_an_unsafe_context_into_a_backend_error(tmp_path):
    ctx = make_context(tmp_path, "fgo-lepton", {"context": "--all"})
    with pytest.raises(BackendError):
        FgoLeptonBackend(ctx)


# ---- C-10 -------------------------------------------------------------------
def test_purge_through_a_symlink_to_an_ancestor_keeps_unrelated_data(tmp_path):
    ctx = make_context(tmp_path, ak.KIND, {})
    backend = ArknightsBackend(ctx)
    data_dir = backend._data_dir()
    broad = tmp_path / "broad-parent"
    (broad / "saves").mkdir(parents=True)
    (broad / "saves" / "slot1").write_text("precious")
    data_dir.rmdir()
    data_dir.symlink_to(broad, target_is_directory=True)
    # The recorded prefix lives below the link target, so ``broad`` is its ancestor.
    ctx.set("installed_prefix", str(broad / "prefix"))

    backend.uninstall(purge=True)

    assert not data_dir.exists() and not data_dir.is_symlink()
    assert (broad / "saves" / "slot1").read_text() == "precious"
