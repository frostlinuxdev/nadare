# SPDX-License-Identifier: GPL-3.0-or-later
"""Packaged-build fixes: managed updates, host shortcuts, display hint, logging, wheel data."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from nadare import app
from nadare.core import logging as nadare_logging
from nadare.core import shortcuts, updatecheck


# ---------------------------------------------------------------------------
# F-03: package managers own updates
# ---------------------------------------------------------------------------
def test_managed_updates_marker(monkeypatch):
    monkeypatch.setenv("NADARE_MANAGED_UPDATES", "1")
    assert updatecheck.managed_updates()


def test_managed_updates_flatpak(monkeypatch):
    monkeypatch.delenv("NADARE_MANAGED_UPDATES", raising=False)
    monkeypatch.setattr(updatecheck.sandbox, "in_flatpak", lambda: True)
    assert updatecheck.managed_updates()


def test_updates_are_not_managed_for_an_appimage(monkeypatch):
    monkeypatch.delenv("NADARE_MANAGED_UPDATES", raising=False)
    monkeypatch.setattr(updatecheck.sandbox, "in_flatpak", lambda: False)
    assert not updatecheck.managed_updates()


# ---------------------------------------------------------------------------
# F-04: Flatpak shortcuts target the host, not the private sandbox home
# ---------------------------------------------------------------------------
def test_flatpak_applications_dir_uses_host_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: True)
    host = tmp_path / "host"
    monkeypatch.setenv("HOST_XDG_DATA_HOME", str(host))
    assert shortcuts._applications_dir(None) == host / "applications"


def test_flatpak_steam_scan_uses_host_home(monkeypatch, tmp_path):
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: True)
    host_home = tmp_path / "host-home"
    config = host_home / ".local" / "share" / "Steam" / "userdata" / "42" / "config"
    config.mkdir(parents=True)
    monkeypatch.setattr(shortcuts, "_host_home", lambda: host_home)
    monkeypatch.setattr(shortcuts, "_host_path", lambda path: path)
    assert config / "shortcuts.vdf" in shortcuts.find_steam_shortcut_files()


# ---------------------------------------------------------------------------
# F-06: the no-display hint names the command the user actually has
# ---------------------------------------------------------------------------
def _no_display(monkeypatch):
    from nadare import gui

    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "APPIMAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gui.sandbox, "in_flatpak", lambda: False)
    return gui


def test_no_display_hint_uses_the_appimage_path(monkeypatch):
    gui = _no_display(monkeypatch)
    monkeypatch.setenv("APPIMAGE", "/opt/Nadare-0.1.AppImage")
    with pytest.raises(SystemExit) as exc:
        gui._require_display()
    assert "/opt/Nadare-0.1.AppImage list" in str(exc.value)


def test_no_display_hint_uses_flatpak_run(monkeypatch):
    gui = _no_display(monkeypatch)
    monkeypatch.setattr(gui.sandbox, "in_flatpak", lambda: True)
    with pytest.raises(SystemExit) as exc:
        gui._require_display()
    assert f"flatpak run {shortcuts.FLATPAK_ID}" in str(exc.value)


def test_no_display_hint_uses_the_invoked_program(monkeypatch):
    gui = _no_display(monkeypatch)
    monkeypatch.setattr(gui.sys, "argv", ["/usr/bin/nadare"])
    with pytest.raises(SystemExit) as exc:
        gui._require_display()
    assert "/usr/bin/nadare list" in str(exc.value)


# ---------------------------------------------------------------------------
# F-07: a read-only state dir falls back to stderr without tracebacks
# ---------------------------------------------------------------------------
def test_read_only_state_dir_logs_to_stderr_once(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o500)
    if os.access(logs, os.W_OK):
        # root (as in CI containers) ignores directory permissions: nothing is read-only.
        logs.chmod(0o700)
        pytest.skip("directory permissions do not apply to this user")
    nadare_logging.reset()
    try:
        logger = nadare_logging.setup(filename=logs / "nadare.log")
        assert nadare_logging._configured is False
        logger.warning("hello from nadare")
        logger.warning("second line")
        err = capsys.readouterr().err
        assert "hello from nadare" in err
        assert "second line" in err
        assert "--- Logging error ---" not in err
        assert "Traceback" not in err
        assert nadare_logging._configured is False
    finally:
        logs.chmod(0o700)
        nadare_logging.reset()


def test_logging_retries_the_file_after_it_becomes_writable(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o500)
    nadare_logging.reset()
    try:
        logger = nadare_logging.setup(filename=logs / "nadare.log")
        logger.warning("cannot write yet")
        logs.chmod(0o700)
        logger = nadare_logging.setup(filename=logs / "nadare.log")
        assert nadare_logging._configured is True
        logger.info("written now")
        for handler in list(logger.handlers):
            handler.flush()
        assert "written now" in (logs / "nadare.log").read_text(encoding="utf-8")
    finally:
        logs.chmod(0o700)
        nadare_logging.reset()


# ---------------------------------------------------------------------------
# F-09: running as root is called out in plain words
# ---------------------------------------------------------------------------
def test_root_run_is_warned_about_once(monkeypatch, capsys):
    monkeypatch.setattr(app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(app, "_root_warned", False)
    app.warn_if_root()
    app.warn_if_root()
    err = capsys.readouterr().err
    assert err.count("/root") == 1
    assert "normal desktop user" in err


def test_normal_user_gets_no_root_warning(monkeypatch, capsys):
    monkeypatch.setattr(app.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(app, "_root_warned", False)
    app.warn_if_root()
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# F-08: the pip wheel carries the locale catalogue
# ---------------------------------------------------------------------------
def _build_python() -> str:
    for candidate in {sys.executable, shutil.which("python3"), "/usr/bin/python3"}:
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-c", "import setuptools, wheel"],
            capture_output=True,
            timeout=30,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("no interpreter with setuptools and wheel available")


def test_wheel_includes_locale_assets(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(repo / "pyproject.toml", work / "pyproject.toml")
    shutil.copy(repo / "README.md", work / "README.md")
    shutil.copytree(repo / "src", work / "src")
    out = tmp_path / "wheel"
    out.mkdir()
    subprocess.run(
        [
            _build_python(),
            "-c",
            "import sys; from setuptools import build_meta; "
            "print(build_meta.build_wheel(sys.argv[1]))",
            str(out),
        ],
        check=True,
        cwd=work,
        timeout=120,
    )
    wheel = next(out.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert any(name.endswith("assets/locales/en.json") for name in names), names
