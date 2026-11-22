# SPDX-License-Identifier: GPL-3.0-or-later
"""Uninstall offers to remove FGO's SELinux policy module (#1555).

The module is only offered when this build has the FGO backend and the module is
actually loaded. Removing it needs the new ``--remove-selinux-module`` flag; the
tests never run ``semodule`` or ``pkexec`` for real.
"""

from __future__ import annotations

import subprocess

import pytest

from nadare import app
from nadare.core import fgo_selinux, paths, uninstall_all
from nadare.core.config import Config


def _result(argv, code: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(argv, code, out, err)


def _which(*found: str):
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in found else None

    return which


def _runner(calls: list[list[str]], out: str = "", code: int = 0):
    def run(argv, **_kwargs):
        calls.append(list(argv))
        return _result(argv, code=code, out=out)

    return run


# ---- when the module is offered -------------------------------------------------------------
def test_a_build_without_the_fgo_backend_never_asks_selinux(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: False)
    assert fgo_selinux.available(run=_runner(calls), which=_which("semodule", "pkexec")) is False
    assert calls == []


def test_a_missing_semodule_means_the_module_is_not_loaded(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)
    assert fgo_selinux.module_loaded(run=_runner(calls), which=_which()) is False
    assert calls == []


@pytest.mark.parametrize(
    ("out", "code", "expected"),
    [
        ("lepton_binder\n", 0, True),
        ("lepton_binder\t1.2\nother_module\n", 0, True),
        ("other_module\n", 0, False),
        ("lepton_binder\n", 1, False),
    ],
)
def test_the_module_list_is_parsed_strictly(monkeypatch, out, code, expected):
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)
    runner = _runner([], out=out, code=code)
    assert fgo_selinux.module_loaded(run=runner, which=_which("semodule")) is expected


def test_available_needs_both_the_backend_and_the_loaded_module(monkeypatch):
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)
    loaded = _runner([], out="lepton_binder\n")
    assert fgo_selinux.available(run=loaded, which=_which("semodule")) is True
    missing = _runner([], out="other_module\n")
    assert fgo_selinux.available(run=missing, which=_which("semodule")) is False


# ---- the exact command ----------------------------------------------------------------------
def test_the_command_never_runs_a_user_writable_script_as_root(tmp_path):
    # The helper script lives in the user's data folder; anything running as the user
    # could change it before pkexec runs it as root. Only the system semodule is used.
    prefix = tmp_path / "lepton"
    bin_dir = prefix / "lepton-x86_64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "setup-host-selinux.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    for profile in ({"prefix": str(prefix)}, {"prefix": str(tmp_path / "gone")}, None):
        assert fgo_selinux.removal_argv(profile) == ["pkexec", "semodule", "-r", "lepton_binder"]
        assert fgo_selinux.removal_command(profile) == "pkexec semodule -r lepton_binder"


# ---- running it -----------------------------------------------------------------------------
def test_remove_never_runs_when_the_module_is_not_loaded(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)
    ok, message = fgo_selinux.remove(
        run=_runner(calls, out="other_module\n"), which=_which("semodule", "pkexec")
    )
    assert ok is False
    assert "not loaded" in message
    assert calls == [["semodule", "-l"]]


def test_remove_asks_pkexec_only_when_the_module_is_loaded(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv == ["semodule", "-l"]:
            return _result(argv, out="lepton_binder\n")
        return _result(argv)

    ok, message = fgo_selinux.remove(
        profile={"prefix": str(tmp_path / "not-installed")},
        run=run,
        which=_which("semodule", "pkexec"),
    )
    assert ok is True
    assert "Removed FGO's SELinux policy module" in message
    assert calls[-1] == ["pkexec", "semodule", "-r", "lepton_binder"]


def test_a_missing_pkexec_gives_a_clear_fallback(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return _result(argv, out="lepton_binder\n")

    ok, message = fgo_selinux.remove(
        profile={"prefix": str(tmp_path / "not-installed")},
        run=run,
        which=_which("semodule"),
    )
    assert ok is False
    assert "pkexec" in message
    assert "sudo semodule -r lepton_binder" in message
    assert calls == [["semodule", "-l"]]  # nothing was run


def test_a_single_removed_game_only_gets_the_command(monkeypatch):
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux, "removal_command", lambda profile=None: "pkexec semodule -r lepton_binder"
    )
    note = fgo_selinux.single_game_note({"backend": "fgo-lepton"})
    assert "pkexec semodule -r lepton_binder" in note
    assert "not changed" in note
    assert fgo_selinux.single_game_note({"backend": "wine"}) == ""


# ---- the command line -----------------------------------------------------------------------
def _empty_config() -> None:
    cfg = Config.load(paths.config_file())
    cfg.save()


def test_the_plan_prints_the_command_and_runs_nothing(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile) or (True, "Removed."),
    )
    code = app.run_cli(["uninstall-all"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pkexec semodule -r lepton_binder" in out
    assert called == []


def test_the_flag_without_yes_only_plans(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile) or (True, "Removed."),
    )
    code = app.run_cli(["uninstall-all", "--remove-selinux-module"])
    out = capsys.readouterr().out
    assert code == 0
    assert "on Also remove" in out
    assert called == []


def test_a_refused_uninstall_does_not_remove_the_module(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile) or (True, "Removed."),
    )
    monkeypatch.setattr(
        uninstall_all,
        "run",
        lambda *a, **k: uninstall_all.Report([], [], ["a game is running"], refused=True),
    )
    code = app.run_cli(["uninstall-all", "--yes", "--remove-selinux-module"])
    out = capsys.readouterr().out
    assert code == 1
    assert called == []
    assert "kept FGO's SELinux policy module" in out


def test_yes_without_the_flag_keeps_the_module_and_prints_the_command(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile) or (True, "Removed."),
    )
    monkeypatch.setattr(
        uninstall_all, "run", lambda *a, **k: uninstall_all.Report([], [], [])
    )
    code = app.run_cli(["uninstall-all", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert called == []
    assert "kept FGO's SELinux policy module" in out
    assert "pkexec semodule -r lepton_binder" in out


def test_the_new_flag_runs_the_removal(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile)
        or (True, "Removed FGO's SELinux policy module (lepton_binder)."),
    )
    monkeypatch.setattr(
        uninstall_all, "run", lambda *a, **k: uninstall_all.Report([], [], [])
    )
    code = app.run_cli(["uninstall-all", "--yes", "--remove-selinux-module"])
    out = capsys.readouterr().out
    assert code == 0
    assert called == [None]
    assert "Removed FGO's SELinux policy module" in out


def test_a_failed_removal_is_reported_and_fails_the_command(monkeypatch, capsys):
    _empty_config()
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: (
            False,
            "pkexec was not found. Run this as root: sudo semodule -r lepton_binder",
        ),
    )
    monkeypatch.setattr(
        uninstall_all, "run", lambda *a, **k: uninstall_all.Report([], [], [])
    )
    code = app.run_cli(["uninstall-all", "--yes", "--remove-selinux-module"])
    captured = capsys.readouterr()
    assert code == 1
    assert "pkexec was not found" in (captured.out + captured.err)


def test_the_flag_is_quiet_when_the_module_is_not_loaded(monkeypatch, capsys):
    _empty_config()
    called: list[object] = []
    monkeypatch.setattr(fgo_selinux, "available", lambda: False)
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: called.append(profile) or (True, "Removed."),
    )
    monkeypatch.setattr(
        uninstall_all, "run", lambda *a, **k: uninstall_all.Report([], [], [])
    )
    code = app.run_cli(["uninstall-all", "--yes", "--remove-selinux-module"])
    out = capsys.readouterr().out
    assert code == 0
    assert called == []
    assert "not loaded" in out


# ---- removing one game ----------------------------------------------------------------------
def test_removing_one_fgo_game_prints_the_command_and_never_runs_it(monkeypatch, capsys):
    from nadare.core import removal

    cfg = Config.load(paths.config_file())
    cfg.add_profile("fgo", backend="fgo-lepton", title="FGO")
    cfg.save()
    monkeypatch.setattr(
        removal, "remove_game", lambda *a, **k: removal.RemovalResult(profile="fgo")
    )
    ran: list[object] = []
    monkeypatch.setattr(
        fgo_selinux,
        "single_game_note",
        lambda profile: (
            "FGO's SELinux policy module (lepton_binder) was not changed. "
            "To remove it, run:\npkexec semodule -r lepton_binder"
        ),
    )
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: ran.append(profile) or (True, "Removed."),
    )
    code = app.run_cli(
        ["-c", str(paths.config_file()), "remove", "-p", "fgo", "--yes"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "pkexec semodule -r lepton_binder" in out
    assert ran == []


def test_module_names_in_stderr_are_not_a_loaded_module(monkeypatch):
    monkeypatch.setattr(fgo_selinux, "backend_available", lambda: True)
    assert not fgo_selinux.available(
        run=lambda argv, **kw: _result(argv, err="lepton_binder diagnostic\n"),
        which=_which("semodule"),
    )
