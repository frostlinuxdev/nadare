# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1417 phase 2d: uninstall-all offers umu's own folder separately, never by default.

``~/.local/share/umu`` holds the Steam Runtime umu downloads (1.5-4.1 GB measured). Steam,
Lutris, Heroic, Faugus and the Flatpak share it, so it is never part of the default run.
Nadare's own copy of umu-launcher (``<data>/nadare/umu``) stays in the "runtimes" group.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare import app
from nadare.core import paths, uninstall_all
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path: Path):
    for fn in (paths.data_dir, paths.config_dir, paths.state_dir, paths.cache_dir):
        fn().mkdir(parents=True, exist_ok=True)
    umu = Path.home() / ".local" / "share" / "umu"
    (umu / "steamrt3").mkdir(parents=True)
    (umu / "steamrt3" / "run").write_text("#!/bin/sh\n")
    (umu / ".3w3yy4xh").mkdir()  # a download a killed umu left behind
    own = paths.data_dir() / "umu" / "1.4.4"
    own.mkdir(parents=True)
    (own / "umu-run").write_text("#!/bin/sh\n")
    cfg = Config.load(paths.config_file())
    cfg.save()
    return cfg, tmp_path, umu, own


def _run(cfg, tmp, **kwargs):
    return uninstall_all.run(cfg, applications_dir=tmp / "apps", desktop_dir=tmp / "desk", **kwargs)


def test_the_plan_offers_umus_folder_off_by_default_with_the_shared_note(env) -> None:
    cfg, tmp, umu, own = env
    groups = {g.key: g for g in uninstall_all.plan(cfg)}
    assert groups["umu"].default is False
    assert groups["umu"].paths == (umu,)
    assert "shared" in groups["umu"].note and "Steam" in groups["umu"].note
    assert umu not in groups["runtimes"].paths


def test_the_runtimes_label_names_nadares_own_umu_copy(env) -> None:
    cfg, tmp, umu, own = env
    groups = {g.key: g for g in uninstall_all.plan(cfg)}
    assert "managed umu folder" not in groups["runtimes"].label
    assert "umu-launcher" in groups["runtimes"].label
    assert paths.data_dir() / "umu" in groups["runtimes"].paths


def test_no_umu_group_paths_when_the_folder_is_missing(env) -> None:
    cfg, tmp, umu, own = env
    import shutil

    shutil.rmtree(umu)
    groups = {g.key: g for g in uninstall_all.plan(cfg)}
    assert groups["umu"].paths == ()


def test_the_default_run_keeps_umus_folder(env) -> None:
    cfg, tmp, umu, own = env
    report = _run(cfg, tmp)
    assert not report.refused
    assert (umu / "steamrt3" / "run").is_file() and (umu / ".3w3yy4xh").is_dir()
    assert not own.exists(), "Nadare's own umu copy still goes with the runtimes"


def test_umus_folder_goes_when_chosen(env) -> None:
    cfg, tmp, umu, own = env
    report = _run(cfg, tmp, umu=True)
    assert not umu.exists()
    assert umu in report.removed


def test_umus_folder_is_kept_while_something_runs_from_it(env, monkeypatch) -> None:
    cfg, tmp, umu, own = env
    monkeypatch.setattr(uninstall_all, "_running_from", lambda root: ["pv-adverb (1234)"])
    report = _run(cfg, tmp, umu=True)
    assert (umu / "steamrt3" / "run").is_file()
    assert any("pv-adverb" in w and "umu" in w for w in report.warnings)


def test_running_from_finds_a_process_whose_program_is_inside(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess

    folder = tmp_path / "rt"
    folder.mkdir()
    # The copy keeps the name "sleep": a multicall coreutils picks its tool from argv[0]
    # (busybox) or from its own file name (the CI runner's), and exits at once otherwise.
    exe = folder / "sleep"
    shutil.copy2(shutil.which("sleep") or "/bin/sleep", exe)
    proc = subprocess.Popen([str(exe), "30"])
    try:
        found = uninstall_all._running_from(folder)
        assert any(str(proc.pid) in item for item in found)
        assert uninstall_all._running_from(tmp_path / "elsewhere") == []
    finally:
        proc.kill()
        proc.wait()
    assert os.path.isdir(folder)


def test_a_umu_folder_that_is_a_link_is_not_walked(env, tmp_path: Path) -> None:
    cfg, tmp, umu, own = env
    import shutil

    shutil.rmtree(umu)
    real = tmp_path / "elsewhere-umu"
    (real / "steamrt3").mkdir(parents=True)
    umu.symlink_to(real)
    _run(cfg, tmp, umu=True)
    assert (real / "steamrt3").is_dir()


def test_the_command_shows_the_umu_group_off_and_takes_with_umu(env, capsys, monkeypatch) -> None:
    cfg, tmp, umu, own = env
    code = app.run_cli(["-c", str(paths.config_file()), "uninstall-all"])
    out = capsys.readouterr().out
    assert code == 0
    line = next(line for line in out.splitlines() if "umu" in line and "Steam Runtime" in line)
    assert line.startswith("off ")
    monkeypatch.setattr(uninstall_all, "_applications_dir", lambda: tmp / "apps")
    monkeypatch.setattr(uninstall_all, "_desktop_dir", lambda: tmp / "desk")
    argv = ["-c", str(paths.config_file()), "uninstall-all", "--with-umu", "--yes"]
    assert app.run_cli(argv) == 0
    assert not umu.exists()


def test_with_umu_shows_the_group_on_in_the_plan(env, capsys) -> None:
    cfg, tmp, umu, own = env
    app.run_cli(["-c", str(paths.config_file()), "uninstall-all", "--with-umu"])
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "Steam Runtime" in line)
    assert line.startswith("on ")
    assert umu.is_dir()
