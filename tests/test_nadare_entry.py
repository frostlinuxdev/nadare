# SPDX-License-Identifier: GPL-3.0-or-later
"""The frozen-app entry shim: ``--cli`` alone must run the CLI, not the GUI."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[1] / "packaging" / "nadare_entry.py"


def _load():
    spec = importlib.util.spec_from_file_location("nadare_entry_under_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bare_cli_flag_runs_cli_not_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _load()
    monkeypatch.setattr(entry, "_run_gui", lambda: pytest.fail("GUI started"))
    seen: list = []
    monkeypatch.setattr("nadare.__main__.main", lambda argv: seen.append(argv) or 0)
    assert entry.main(["--cli"]) == 0
    assert seen == [[]]


def test_gui_path_ensures_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import nadare.gui as gui
    from nadare.core import tls

    calls: list = []

    class Stop(Exception):
        pass

    def stop():
        raise Stop

    monkeypatch.setattr(tls, "ensure_ca_bundle", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(gui, "_application", stop)
    entry = _load()
    with pytest.raises(Stop):
        entry._run_gui()
    assert calls == [1]


def test_cli_path_ensures_ca_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from nadare import app
    from nadare.core import tls

    calls: list = []
    monkeypatch.setattr(tls, "ensure_ca_bundle", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "k"))
    app.run_cli(["--config", str(tmp_path / "cfg.json"), "list"])
    assert calls
