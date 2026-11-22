# SPDX-License-Identifier: GPL-3.0-or-later
"""#1465: status() is a read-only query, so it must not create the profile folder or anything else.

``BackendContext.data_dir()`` made the profile folder on every call, and a Wine game's
``prefix()`` default went through it, so asking a fresh profile "are you installed?" left
``profiles/<name>/`` behind (and failed outright where that folder could not be made).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import backends, paths
from nadare.core.config import Config

_SCANNERS = (
    "nadare.backends.wine",
    "nadare.backends.winegame",
    "nadare.backends.winegame_parts.launching",
    "nadare.backends.steam",
    "nadare.backends.script",
    "nadare.backends.launcher",
)


def _kinds() -> list[str]:
    out = []
    for kind in backends.kinds():
        if kind in backends.RETIRED_KINDS:
            continue
        try:
            backends.get(kind)
        except backends.BackendError:
            continue  # optional kind whose import is unavailable here
        out.append(kind)
    return out


def _tree(root: Path) -> set[str]:
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _no_processes(monkeypatch) -> None:
    import importlib

    for name in _SCANNERS:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(mod, "scan_pids"):
            monkeypatch.setattr(mod, "scan_pids", lambda *a, **k: [], raising=False)


@pytest.mark.parametrize("kind", _kinds())
def test_status_of_a_fresh_profile_writes_nothing(tmp_path, monkeypatch, kind):
    _no_processes(monkeypatch)
    cfg = Config.load(tmp_path / "config.toml")
    cfg.add_profile("sample", backend=kind)
    cfg.save()
    backend = backends.create("sample", cfg)

    before = _tree(tmp_path)
    try:
        backend.status()
    except backends.BackendError:
        pass  # a refusal is fine; what matters is that nothing was written
    after = _tree(tmp_path)

    assert after - before == set(), f"{kind}: status() created {sorted(after - before)}"
    assert not (paths.profiles_dir() / "sample").exists()


def test_status_works_where_the_profile_folder_cannot_be_made(tmp_path, monkeypatch):
    """A read-only data root: the query answers instead of failing on a mkdir."""
    from nadare.backends.arknights import ArknightsBackend  # noqa: F401  (registers the kind)

    _no_processes(monkeypatch)
    cfg = Config.load(tmp_path / "config.toml")
    cfg.add_profile("sample", backend="arknights-yostar")
    cfg.save()
    root = paths.profiles_dir()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.parent.chmod(0o500)
    try:
        status = backends.create("sample", cfg).status()
    finally:
        root.parent.chmod(0o700)
    assert status.state == "not_installed"


def test_install_still_creates_the_profile_folder(tmp_path):
    """The creating accessor is unchanged for the paths that write (setup, install)."""
    cfg = Config.load(tmp_path / "config.toml")
    cfg.add_profile("sample", backend="arknights-yostar")
    ctx = backends.BackendContext("sample", cfg.profile("sample"), cfg)
    assert ctx.data_path() == paths.profiles_dir() / "sample"
    assert not ctx.data_path().exists()
    assert ctx.data_dir().is_dir()


def test_czn_session_path_is_where_the_session_is_written(tmp_path):
    """status reads the session through the pure path; it must be the file launches write."""
    cfg = Config.load(tmp_path / "config.toml")
    cfg.add_profile("sample", backend="stove-czn")
    backend = backends.create("sample", cfg)
    assert backend._session_path() == backend._session()


def test_generic_wine_commands_still_make_the_profile_folder(tmp_path):
    """Wine creates the prefix but not its parent: the command builder makes the profile folder."""
    cfg = Config.load(tmp_path / "config.toml")
    cfg.add_profile("sample", backend="wine")
    backend = backends.create("sample", cfg)
    assert not (paths.profiles_dir() / "sample").exists()
    try:
        backend._prefix_command(["wineboot", "-u"])
    except backends.BackendError:
        pass  # no runner on this host is fine; the folder comes first
    assert (paths.profiles_dir() / "sample").is_dir()
