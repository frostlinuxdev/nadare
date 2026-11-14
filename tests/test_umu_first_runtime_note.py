# SPDX-License-Identifier: GPL-3.0-or-later
"""The first launch through umu downloads the Steam Runtime: say so (#1338 follow-up).

Since #1340 every Proton launch on a desktop goes through umu. A player who used to run
``proton run`` (32-bit libraries, no system umu) has no Steam Runtime on disk yet, so the first
Play after the update makes umu download it before the game starts. The page only said
"launching <exe>" for minutes, which reads as a hang. Now a step names the one-time download
when the runtime is not on disk, and nothing extra is said once it is.
"""

from __future__ import annotations

from pathlib import Path

from _backend_helpers import make_context
from test_backends_arknights import _backend, _install_launcher

from nadare.backends import wine as generic_wine
from nadare.backends import winegame as wg
from nadare.core import umu, userns
from nadare.core.events import Event


def _runtime_on_disk(data_home: Path) -> None:
    srt = (
        data_home / "umu" / "steamrt3" / "pressure-vessel" / "libexec"
        / "steam-runtime-tools-0" / "srt-bwrap"
    )
    srt.parent.mkdir(parents=True)
    srt.write_text("")
    srt.chmod(0o755)


def _steps(events: list[Event]) -> list[str]:
    return [event.message for event in events if event.kind == "step"]


def _winegame(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _install_launcher(prefix)
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    backend = _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"})
    events: list[Event] = []
    backend.ctx.emit = events.append
    return backend, events


def test_runtime_downloaded_reads_the_umu_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert umu.runtime_downloaded() is False
    _runtime_on_disk(tmp_path / "data")
    assert umu.runtime_downloaded() is True


def test_first_launch_without_the_runtime_says_it_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    backend, events = _winegame(tmp_path, monkeypatch)
    argv, _env, _target = backend.build_launch()
    assert argv[0] == "/usr/bin/umu-run"
    notes = [step for step in _steps(events) if "Steam Runtime" in step]
    assert notes == [umu.FIRST_RUNTIME_NOTE]
    assert "once" in umu.FIRST_RUNTIME_NOTE and "minutes" in umu.FIRST_RUNTIME_NOTE


def test_runtime_on_disk_adds_no_note(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _runtime_on_disk(tmp_path / "data")
    backend, events = _winegame(tmp_path, monkeypatch)
    backend.build_launch()
    assert not [step for step in _steps(events) if "Steam Runtime" in step]


def test_no_umu_adds_no_download_note(tmp_path, monkeypatch):
    """The refusal message is the news there; a "downloading" line would be false."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    backend, events = _winegame(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: None)
    try:
        backend.build_launch()
    except Exception:  # noqa: BLE001 - the refusal itself is tested in test_umu_always
        pass
    assert not [step for step in _steps(events) if "Steam Runtime" in step]


def test_generic_wine_backend_says_it_too(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    ctx = make_context(
        tmp_path, "wine", {"prefix": str(tmp_path / "pfx"), "runner": "MyProton"}
    )
    events: list[Event] = []
    ctx.emit = events.append
    backend = generic_wine.WineBackend(ctx)
    monkeypatch.setattr(backend, "_runner_for_build", lambda runner: proton)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    argv, _env = backend._prefix_command(["game.exe"])
    assert argv[0] == "/usr/bin/umu-run"
    assert umu.FIRST_RUNTIME_NOTE in _steps(events)
