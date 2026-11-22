# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for audit findings C-06, C-08, C-09 and C-10."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

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
        adapter.cmd_status("--all")
    with pytest.raises(LeptonError):
        adapter.cmd_verify("fgo", "--all")
    assert adapter.cmd_verify("other")[2] == "other"


def test_fgo_backend_turns_an_unsafe_context_into_a_backend_error(tmp_path):
    ctx = make_context(tmp_path, "fgo-lepton", {"context": "--all"})
    with pytest.raises(BackendError):
        FgoLeptonBackend(ctx)

