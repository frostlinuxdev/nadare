# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the lepton-fgo adapter and its Nadare backend."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.fgo import lepton
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

# ---------------------------------------------------------------------------
# representative CLI output written from the documented formats
# ---------------------------------------------------------------------------

STATUS_RUNNING = """\
context:   fgo
container: lepton-fgo (running)
fgo:       2.93.0
venus:     running
avahi:     stopped
"""

STATUS_STOPPED = """\
context:   fgo
container: lepton-fgo (stopped)
venus:     stopped
avahi:     stopped
"""

# A context that exists but has never been fully set up: only the container line.
STATUS_NOT_INSTALLED = ""

# Unknown keys / free-form garbage: must not raise and must not be captured.
STATUS_MALFORMED = """\
this is not a status report
context fgo
container: lepton-fgo (running)
totally-new-key: whatever
fgo:       2.93.0
"""

CHECK_UPDATE_UP_TO_DATE = """\
installed: 2.93.0
newest:    2.93.0
up to date
"""

CHECK_UPDATE_AVAILABLE = """\
installed: 2.91.0
newest:    2.93.0
update available
"""

CHECK_UPDATE_MALFORMED = """\
garbage line without a colon
installed::2.93.0
some: other
"""

CHECK_HOST_OK = """\
[*] checking host
OK: all required pieces present.
getenforce: Permissive
"""

CHECK_HOST_MISSING = """\
MISSING:
  - bash
  - curl
[!] install them
getenforce: Enforcing
"""

CONFIG_TEXT = """\
# comment
export LEPTON_CONTEXT='fgo'
SIZE=1280x720
export MODE=strict
EMPTY=
quoted="hello world"
"""


# ---------------------------------------------------------------------------
# parse_status
# ---------------------------------------------------------------------------
class TestParseStatus:
    def test_installed_running(self):
        out = lepton.parse_status(STATUS_RUNNING)
        assert out == {
            "context": "fgo",
            "container": "lepton-fgo (running)",
            "fgo": "2.93.0",
            "venus": "running",
            "avahi": "stopped",
        }

    def test_installed_stopped_has_no_fgo(self):
        out = lepton.parse_status(STATUS_STOPPED)
        assert out["container"] == "lepton-fgo (stopped)"
        assert out["fgo"] is None
        assert out["venus"] == "stopped"

    def test_not_installed_empty_output(self):
        out = lepton.parse_status(STATUS_NOT_INSTALLED)
        assert out == {
            "context": None,
            "container": None,
            "fgo": None,
            "venus": None,
            "avahi": None,
        }

    def test_malformed_and_unknown_keys(self):
        out = lepton.parse_status(STATUS_MALFORMED)
        # Known keys are captured ...
        assert out["container"] == "lepton-fgo (running)"
        assert out["fgo"] == "2.93.0"
        # ... unknown keys are ignored, and a colon-less line is skipped.
        assert set(out) == {"context", "container", "fgo", "venus", "avahi"}
        assert out["context"] is None


# ---------------------------------------------------------------------------
# parse_check_update
# ---------------------------------------------------------------------------
class TestParseCheckUpdate:
    def test_up_to_date(self):
        out = lepton.parse_check_update(CHECK_UPDATE_UP_TO_DATE)
        assert out == {"installed": "2.93.0", "newest": "2.93.0", "available": False}

    def test_update_available(self):
        out = lepton.parse_check_update(CHECK_UPDATE_AVAILABLE)
        assert out == {"installed": "2.91.0", "newest": "2.93.0", "available": True}

    def test_malformed_returns_defaults(self):
        out = lepton.parse_check_update(CHECK_UPDATE_MALFORMED)
        # "installed::2.93.0" still starts with ``installed:`` -> value ":2.93.0".
        assert out["installed"] == ":2.93.0"
        assert out["newest"] is None
        assert out["available"] is None


# ---------------------------------------------------------------------------
# other parsers
# ---------------------------------------------------------------------------
class TestOtherParsers:
    def test_check_host_ok(self):
        out = lepton.parse_check_host(CHECK_HOST_OK)
        assert out["ok"] is True
        assert out["missing"] == []
        assert out["selinux_enforcing"] is False

    def test_check_host_missing_and_enforcing(self):
        out = lepton.parse_check_host(CHECK_HOST_MISSING)
        assert out["ok"] is False
        assert out["missing"] == ["bash", "curl"]
        assert out["selinux_enforcing"] is True

    def test_parse_config(self):
        out = lepton.parse_config(CONFIG_TEXT)
        assert out == {
            "LEPTON_CONTEXT": "fgo",
            "SIZE": "1280x720",
            "MODE": "strict",
            "EMPTY": "",
            "quoted": "hello world",
        }


# ---------------------------------------------------------------------------
# LeptonFgo locating + parsed verbs
# ---------------------------------------------------------------------------
class TestLeptonFgo:
    def _adapter(self, tmp_path: Path, **kwargs) -> lepton.LeptonFgo:
        return lepton.LeptonFgo(prefix=tmp_path / "prefix", config_dir=tmp_path / "conf", **kwargs)

    def test_status_when_not_installed(self, tmp_path, monkeypatch):
        adapter = self._adapter(tmp_path)
        # The CLI does not exist, so status() must never spawn a subprocess.
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        out = adapter.status()
        assert out["context"] == "fgo"
        assert out["container"] is None
        assert called == []

    def test_status_parses_cli_output(self, tmp_path):
        adapter = self._adapter(tmp_path)
        adapter.bin_dir.mkdir(parents=True)
        adapter.lepton_fgo.write_text("#!/bin/sh\n")
        adapter.conf_path.parent.mkdir(parents=True)
        adapter.conf_path.write_text("")
        adapter.run = lambda cmd, timeout=60: subprocess.CompletedProcess(  # type: ignore[assignment]
            cmd, 0, STATUS_RUNNING, ""
        )
        out = adapter.status()
        assert out["fgo"] == "2.93.0"
        assert adapter.installed is True

    def test_config_empty_when_not_installed(self, tmp_path):
        adapter = self._adapter(tmp_path)
        assert adapter.config() == {}

    def test_installed_requires_both_conf_and_binary(self, tmp_path):
        adapter = self._adapter(tmp_path)
        adapter.conf_path.parent.mkdir(parents=True)
        adapter.conf_path.write_text("")
        assert adapter.installed is False
        adapter.bin_dir.mkdir(parents=True)
        adapter.lepton_fgo.write_text("")
        assert adapter.installed is True

    def test_cmd_game_launch_uses_package_and_activity(self, tmp_path):
        adapter = self._adapter(tmp_path)
        cmd = adapter.cmd_game_launch()
        assert cmd == [str(adapter.lepton_game), "fgo", lepton.FGO_PKG, lepton.FGO_ACTIVITY]

    def test_cmd_update_force(self, tmp_path):
        adapter = self._adapter(tmp_path)
        assert adapter.cmd_update()[-1] == "fgo"
        assert adapter.cmd_update(force=True) == [
            str(adapter.lepton_fgo),
            "update",
            "fgo",
            "--force",
        ]


# ---------------------------------------------------------------------------
# FgoLeptonBackend
# ---------------------------------------------------------------------------
def test_backend_status_not_installed(tmp_path):
    prefix = tmp_path / "lepton-prefix"
    ctx = make_context(tmp_path, "fgo-lepton", {"prefix": str(prefix)})
    backend = FgoLeptonBackend(ctx)
    status = backend.status()
    assert status.state == "not_installed"
    assert status.running is False
    assert status.version is None
    assert status.message


def test_backend_status_installed(tmp_path):
    prefix = tmp_path / "lepton-prefix"
    bin_dir = prefix / "lepton-x86_64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "lepton-fgo").write_text("#!/bin/sh\n")
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "fgo.conf").write_text("")

    class FakeAdapter:
        installed = True

        def status(self=None):
            return {
                "context": "fgo",
                "container": "lepton-fgo (stopped)",
                "fgo": "2.93.0",
                "venus": "stopped",
                "avahi": "stopped",
            }

    ctx = make_context(tmp_path, "fgo-lepton", {"prefix": str(prefix)})
    backend = FgoLeptonBackend(ctx)
    backend.adapter = FakeAdapter()  # type: ignore[assignment]
    status = backend.status()
    assert status.state == "installed"
    assert status.version == "2.93.0"
    assert status.running is False


def test_backend_status_running_via_venus(tmp_path):
    ctx = make_context(tmp_path, "fgo-lepton", {"prefix": "/nonexistent"})
    backend = FgoLeptonBackend(ctx)

    class FakeAdapter:
        installed = True

        def status(self=None):
            return {
                "context": "fgo",
                "container": "lepton-fgo (running)",
                "fgo": "2.93.0",
                "venus": "running",
                "avahi": "stopped",
            }

    backend.adapter = FakeAdapter()  # type: ignore[assignment]
    status = backend.status()
    assert status.running is True
    assert status.state == "running"


def test_lepton_defaults_use_host_dirs_inside_flatpak(tmp_path, monkeypatch) -> None:
    from nadare.core import sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.delenv("LEPTON_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HOST_XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOST_XDG_CONFIG_HOME", str(tmp_path / "hostcfg"))
    assert lepton.default_prefix() == tmp_path / ".local/share/lepton-nv"
    assert lepton.default_config_dir() == tmp_path / "hostcfg/lepton-nv"


def test_lepton_defaults_follow_our_xdg_outside_flatpak(monkeypatch) -> None:
    from nadare.core import paths, sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.delenv("LEPTON_CONFIG_DIR", raising=False)
    assert lepton.default_prefix() == paths.data_dir().parent / "lepton-nv"
    assert lepton.default_config_dir() == paths.config_dir().parent / "lepton-nv"


# ---------------------------------------------------------------------------
# M1: only explicitly trusted locations are probed for install.sh
# ---------------------------------------------------------------------------
def test_repo_ignores_cwd_and_home_checkouts(tmp_path, monkeypatch) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "install.sh").write_text("#!/bin/sh\necho pwned\n")
    home_checkout = Path.home() / "lepton-fgo"
    home_checkout.mkdir(parents=True)
    (home_checkout / "install.sh").write_text("#!/bin/sh\n")
    monkeypatch.chdir(hostile)

    adapter = lepton.LeptonFgo(prefix=tmp_path / "prefix", config_dir=tmp_path / "conf")
    assert adapter.repo == tmp_path / "prefix" / "installer"
    assert adapter.install_sh != hostile / "install.sh"


def test_repo_prefers_an_explicit_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # a cwd checkout must not outrank the explicit one
    (tmp_path / "install.sh").write_text("#!/bin/sh\n")
    explicit = tmp_path / "checkout"
    explicit.mkdir()
    (explicit / "install.sh").write_text("#!/bin/sh\n")
    adapter = lepton.LeptonFgo(
        prefix=tmp_path / "prefix", repo=explicit, config_dir=tmp_path / "conf"
    )
    assert adapter.repo == explicit


# ---------------------------------------------------------------------------
# F1: run() turns a spawn failure into a LeptonError
# ---------------------------------------------------------------------------
def test_run_wraps_non_executable_tool(tmp_path) -> None:
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    tool = tmp_path / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)
    with pytest.raises(lepton.LeptonError, match="missing or not executable"):
        adapter.run([str(tool)])


def test_run_wraps_a_directory(tmp_path) -> None:
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    with pytest.raises(lepton.LeptonError, match="missing or not executable"):
        adapter.run([str(tmp_path)])


# ---------------------------------------------------------------------------
# L4: stream() must not leak a child on a callback error or a silent cancel
# ---------------------------------------------------------------------------
def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def _sleeping_child(pid_file: Path, *, announce: bool) -> list[str]:
    lines = [
        "import os, time",
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))",
    ]
    if announce:
        lines.append("print('hello', flush=True)")
    lines.append("time.sleep(30)")
    return [sys.executable, "-c", "\n".join(lines)]


def test_stream_kills_child_when_callback_raises(tmp_path, monkeypatch) -> None:
    from nadare.core import sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    pid_file = tmp_path / "pid"
    cmd = _sleeping_child(pid_file, announce=True)

    def boom(_line: str) -> None:
        raise RuntimeError("callback exploded")

    with pytest.raises(RuntimeError, match="callback exploded"):
        adapter.stream(cmd, on_line=boom)

    assert pid_file.exists(), "the child should have announced itself before raising"
    assert _wait_gone(int(pid_file.read_text())), "the installer child leaked"


def test_stream_cancel_stops_a_silent_child(tmp_path, monkeypatch) -> None:
    from nadare.core import sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    pid_file = tmp_path / "pid"
    cancel = Cancel()

    def _cancel_when_started() -> None:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.cancel()

    threading.Thread(target=_cancel_when_started, daemon=True).start()
    with pytest.raises(Cancelled):
        adapter.stream(_sleeping_child(pid_file, announce=False), cancel=cancel)

    assert pid_file.exists()
    assert _wait_gone(int(pid_file.read_text())), "a silent child survived cancellation"


# ---------------------------------------------------------------------------
# L3: update() honours the account-safety notice gate
# ---------------------------------------------------------------------------
def test_backend_update_requires_notice_ack(tmp_path) -> None:
    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    with pytest.raises(BackendError) as exc:
        backend.update()
    assert exc.value.code == 2
    assert "account notice" in exc.value.message.lower()


def test_backend_update_with_ack_streams(tmp_path, monkeypatch) -> None:
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    monkeypatch.setattr(backend.adapter, "cmd_update", lambda force=False: ["update"])
    calls: list = []
    monkeypatch.setattr(backend, "_stream", lambda cmd: calls.append(cmd) or 0)
    backend.update()
    assert calls == [["update"]]
