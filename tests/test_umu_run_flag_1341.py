# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden ``nadare --umu-run``: the managed zipapp runs on Nadare's own Python (#1341 item 3).

On NixOS (through ``appimage-run``) there is no ``/usr/bin/python3``, which the managed
umu zipapp's shebang names. The launch must then start Nadare itself with ``--umu-run`` and
let it run the zipapp in-process. With ``python3`` present, the launch is unchanged.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from nadare import app
from nadare.core import protoncmd, steamrun, umu


def _launch_argv(monkeypatch, tmp_path: Path, *, python: bool) -> list[str]:
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: str(umu.managed_path()))
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: python)
    monkeypatch.setattr(umu, "prepare_runtime", lambda *a, **k: True)
    monkeypatch.setattr(protoncmd.userns, "blocks_runtime", lambda: False)
    monkeypatch.setattr(protoncmd.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(protoncmd.pvlocales, "prepare", lambda *a, **k: {})
    return protoncmd.proton_argv(
        ["game.exe"],
        proton=tmp_path / "Proton",
        prefix=tmp_path / "prefix",
        env={},
        emit=lambda event: None,
    )


def test_with_python3_the_launch_still_starts_the_zipapp_directly(monkeypatch, tmp_path):
    argv = _launch_argv(monkeypatch, tmp_path, python=True)
    assert argv == [str(umu.managed_path()), "game.exe"]
    assert umu.SUBCOMMAND not in argv


def test_without_python3_the_launch_starts_nadare_with_umu_run(monkeypatch, tmp_path):
    argv = _launch_argv(monkeypatch, tmp_path, python=False)
    command = umu.command()
    assert argv[: len(command)] == command
    assert command[-1] == "--umu-run"
    assert argv[len(command) :] == ["game.exe"]


def test_a_system_umu_is_never_wrapped(monkeypatch, tmp_path):
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: False)
    assert umu.run_argv("/usr/bin/umu-run", ["game.exe"]) == [
        "/usr/bin/umu-run",
        "game.exe",
    ]


def test_the_steam_installer_uses_the_same_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: False)
    monkeypatch.setattr(umu, "prepare_runtime", lambda *a, **k: False)
    argv, _env = steamrun.umu_run_command(
        umu_run=str(umu.managed_path()),
        proton=tmp_path / "Proton" / "proton",
        compat_data=tmp_path / "compatdata",
        client_path=tmp_path / "Steam",
        args=["setup.exe", "/silent"],
        base_env={"DISPLAY": ":0"},
    )
    command = umu.command()
    assert argv[: len(command)] == command
    assert argv[len(command) :] == ["setup.exe", "/silent"]


def test_command_uses_the_nadare_binary_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert umu.command() == [sys.executable, "--umu-run"]


def _write_zipapp(directory: Path, *, exit_code: int) -> Path:
    """A minimal zipapp that records its arguments and exits with ``exit_code``."""
    target = directory / "umu-run"
    source = (
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['UMU_RECORD']).write_bytes(\n"
        "    b'\\0'.join(arg.encode() for arg in sys.argv[1:]))\n"
        "raise SystemExit(int(os.environ.get('UMU_EXIT', '0')))\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", source)
    target.write_bytes(buffer.getvalue())
    target.chmod(0o755)
    return target


def test_umu_run_dispatch_runs_the_zipapp_with_the_args_and_its_exit_code(
    tmp_path, monkeypatch
):
    zipapp = _write_zipapp(tmp_path, exit_code=7)
    record = tmp_path / "args"
    monkeypatch.setattr(umu, "managed_path", lambda: zipapp)
    monkeypatch.setenv("UMU_RECORD", str(record))
    monkeypatch.setenv("UMU_EXIT", "7")

    assert app.run_cli(["--umu-run", "game.exe", "--windowed"]) == 7
    assert record.read_bytes() == b"game.exe\0--windowed"


def test_umu_run_without_an_installed_copy_reports_and_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(umu, "managed_path", lambda: tmp_path / "missing" / "umu-run")
    assert app.run_cli(["--umu-run", "game.exe"]) == 1


def test_the_sandbox_still_finds_the_game_behind_the_wrapper():
    from nadare.core import gamesandbox

    argv = [sys.executable, "-m", "nadare", "--umu-run", "/games/a/game.exe"]
    assert gamesandbox._proton_program(argv) == Path("/games/a/game.exe")


def test_the_sandbox_sets_up_the_runtime_behind_the_wrapper(monkeypatch):
    from nadare.core import sandboxed, umu

    class _Runner(sandboxed.SandboxMixin):
        def __init__(self):
            self.runs = []

        def info(self, *args, **kwargs):
            pass

        def _run_unwrapped(self, argv, env, *, on_line):
            self.runs.append(argv)
            return 0

    monkeypatch.setattr(umu, "runtime_downloaded", lambda *a: False)
    runner = _Runner()
    runner._runtime_before_sandbox(
        [sys.executable, "-m", "nadare", "--umu-run", "game.exe"], {}
    )
    assert runner.runs == [[sys.executable, "-m", "nadare", "--umu-run", ""]]


def test_umu_run_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        app.run_cli(["--help"])
    assert "--umu-run" not in capsys.readouterr().out


def test_the_check_leaves_no_umu_module_behind(tmp_path):
    """Checking a download without python3 must not import umu into the GUI."""
    candidate = tmp_path / "umu-run"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("umu_probe_1341/__init__.py", "LOADED = True\n")
        archive.writestr("__main__.py", "import umu_probe_1341\nraise SystemExit(0)\n")
    candidate.write_bytes(buffer.getvalue())
    candidate.chmod(0o755)
    path_before = list(sys.path)
    umu._smoke_test(None, candidate)
    assert "umu_probe_1341" not in sys.modules
    assert sys.path == path_before


def test_check_isolates_process_state_and_captures_output(tmp_path, monkeypatch, capsys):
    """A --version probe must not change GUI process state or leak its output."""
    import os

    monkeypatch.setenv("UMU_REVIEW_STATE", "parent")
    candidate = tmp_path / "umu run with spaces"
    with zipfile.ZipFile(candidate, "w") as archive:
        archive.writestr(
            "__main__.py",
            "import os\nos.environ['UMU_REVIEW_STATE'] = 'child'\n"
            "print('umu version probe')\nraise SystemExit(0)\n",
        )
    candidate.chmod(0o755)
    umu._smoke_test(None, candidate)
    assert os.environ["UMU_REVIEW_STATE"] == "parent"
    assert "umu version probe" not in capsys.readouterr().out


def test_frozen_check_is_a_bounded_child_process(tmp_path, monkeypatch):
    from types import SimpleNamespace

    candidate = tmp_path / "candidate with spaces"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        umu, "_run_zipapp", lambda *a: pytest.fail("probe ran in the GUI process")
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(umu.subprocess, "run", run)
    umu._smoke_test(None, candidate)
    assert calls[0][0] == [sys.executable, "-m", "nadare.core.umu", str(candidate)]
    assert calls[0][1]["timeout"] == 30
    assert calls[0][1]["capture_output"] is True


def test_missing_shebang_checks_with_own_python_even_if_path_has_python(tmp_path, monkeypatch):
    from test_umu import _fake_download, _zipapp_payload

    _fake_download(monkeypatch, _zipapp_payload())
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: False)
    monkeypatch.setattr(umu.shutil, "which", lambda *a: "/unusable/python3")
    checks = []
    monkeypatch.setattr(umu, "_smoke_test", lambda python, path: checks.append(python))
    umu.ensure_managed()
    assert checks == [None]


@pytest.mark.parametrize("frozen", [False, True])
def test_sandbox_keeps_the_fallback_zipapp_and_interpreter_files(tmp_path, monkeypatch, frozen):
    from nadare.core import gamesandbox

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    zipapp = home / ".local/share/nadare/umu/1.4.4/umu-run"
    zipapp.parent.mkdir(parents=True)
    zipapp.write_bytes(b"zipapp")
    monkeypatch.setattr(umu, "managed_path", lambda: zipapp)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if frozen:
        resources = home / "Nadare/_internal"
        resources.mkdir(parents=True)
        monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
        expected = [zipapp, resources]
    else:
        venv = home / "launcher/.venv"
        package = home / "launcher/src/nadare"
        venv.mkdir(parents=True)
        (package / "core").mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", str(venv))
        monkeypatch.setattr(umu, "__file__", str(package / "core/umu.py"))
        expected = [zipapp, venv, package]
    prefix = home / "Games/prefix"
    prefix.mkdir(parents=True)
    argv, _env = gamesandbox.wrap(
        [*umu.command(), "game.exe"], {}, prefix=prefix, home=home,
    )
    for path in expected:
        assert any(
            argv[i:i + 3] == ["--ro-bind", str(path), str(path)]
            for i in range(len(argv) - 2)
        ), f"fallback dependency hidden: {path}"
    assert str(zipapp.parent.parent.parent) not in argv
    assert ["--ro-bind", str(home), str(home)] not in [argv[i:i + 3] for i in range(len(argv))]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_private_candidate_check_runs_version_in_a_child(tmp_path, monkeypatch, exit_code):
    import json
    import os

    candidate = _write_zipapp(tmp_path, exit_code=exit_code)
    record = tmp_path / "version args"
    monkeypatch.setenv("UMU_RECORD", str(record))
    monkeypatch.setenv("UMU_EXIT", str(exit_code))
    # Record the child's PID too, so a probe that becomes a no-op cannot pass.
    with zipfile.ZipFile(candidate, "w") as archive:
        archive.writestr(
            "__main__.py",
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['UMU_RECORD']).write_text(\n"
            "    json.dumps([sys.argv, os.getpid()]))\n"
            "raise SystemExit(int(os.environ['UMU_EXIT']))\n",
        )
    if exit_code:
        with pytest.raises(umu.UmuError, match="does not run"):
            umu._smoke_test(None, candidate)
    else:
        umu._smoke_test(None, candidate)
    argv, pid = json.loads(record.read_text())
    assert argv == [str(candidate), "--version"]
    assert pid != os.getpid()
