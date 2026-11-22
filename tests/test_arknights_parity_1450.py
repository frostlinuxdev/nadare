# SPDX-License-Identifier: GPL-3.0-or-later
"""Arknights is treated like Stella Sora, the game that shares its backend (#1450).

Both install and play launcher-free from the Yostar manifest by default. Arknights still had
the Sandbox switched off (only Stella Sora's class turned it on, a335f4c2), could not Pause an
install that the same resumable sync can resume, and the library called both "Yostar launcher"
while the Store said direct download (#1413).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.core import backends, gamesandbox
from nadare.core.directkinds import DIRECT_LABEL
from nadare.gui.game_view_parts.busy import can_pause_kind
from nadare.gui.library import backend_label

YOSTAR = ("arknights-yostar", "stellasora-yostar")


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


def test_both_yostar_games_offer_the_sandbox():
    for kind in YOSTAR:
        assert backends.supports_sandbox(kind), kind


def test_both_yostar_games_can_pause_an_install():
    for kind in YOSTAR:
        assert can_pause_kind(kind), kind


def test_the_library_says_direct_download_like_the_store():
    for kind in YOSTAR:
        assert backend_label(kind) == DIRECT_LABEL, kind


def _arknights(tmp_path: Path, monkeypatch, *, manifest: bool, **profile):
    from nadare.backends import winegame
    from nadare.backends.arknights import ArknightsBackend

    prefix = tmp_path / "prefix"
    exe = write_exe(prefix / "drive_c" / "YostarGames" / "Arknights_EN" / "Arknights.exe", "MZ")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", **profile}
    if manifest:
        values["manifest_file"] = str(tmp_path / "manifest.json")
    backend = ArknightsBackend(make_context(tmp_path, "arknights-yostar", values))
    if not manifest:
        # Nadare's index turned off: no manifest anywhere, the official launcher route.
        monkeypatch.setattr(backend, "manifest_source", lambda: "")
    seen: list[list[str]] = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    patch_everywhere(
        monkeypatch, winegame, "stream", lambda argv, env, **kw: seen.append(argv) or 0
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    return backend, exe, seen


def test_arknights_direct_play_runs_inside_the_sandbox(tmp_path, monkeypatch, has_bwrap):
    backend, exe, seen = _arknights(tmp_path, monkeypatch, manifest=True, secure_mode=True)
    assert backend.direct_mode()
    assert backend.sandbox_default_on  # on by default, like every direct game
    assert backend.launch() == 0
    argv = seen[0]
    assert Path(argv[0]).name == "bwrap" and "--tmpfs" in argv
    assert argv[argv.index("--") + 1 :][-1] == str(exe)


def test_arknights_with_the_sandbox_off_runs_unwrapped(tmp_path, monkeypatch):
    backend, exe, seen = _arknights(tmp_path, monkeypatch, manifest=True, secure_mode=False)
    assert backend.launch() == 0
    assert Path(seen[0][0]).name != "bwrap" and seen[0][-1] == str(exe)


def test_a_b24_arknights_profile_plays_in_the_sandbox_after_the_update(
    tmp_path, monkeypatch, has_bwrap
):
    """User, 2026-11-16: existing Arknights players get the Sandbox too, like every direct game
    did on 2026-11-08. b24 wrote no ``secure_mode`` for Arknights (it had no switch); the new
    code reads that profile with the Sandbox on, and Play runs wrapped."""
    import json

    from nadare.backends import winegame
    from nadare.backends.arknights import ArknightsBackend
    from nadare.core.backends import BackendContext
    from nadare.core.config import Config

    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)  # the shipped default, not conftest's
    prefix = tmp_path / "prefix"
    exe = write_exe(prefix / "drive_c" / "YostarGames" / "Arknights_EN" / "Arknights.exe", "MZ")
    path = tmp_path / "config.json"
    # As b24 saved it: the profile keys Add Game and a finished install wrote.
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "profiles": {
                    "arknights": {
                        "backend": "arknights-yostar",
                        "title": "Arknights",
                        "prefix": str(prefix),
                        "runner": "dwproton",
                        "wine_binary": "/usr/bin/wine",
                        "manifest_file": str(tmp_path / "manifest.json"),
                    }
                },
            }
        )
    )
    cfg = Config.load(path)
    backend = ArknightsBackend(
        BackendContext(name="arknights", profile=cfg.profile("arknights"), config=cfg)
    )
    assert "secure_mode" not in cfg.profile("arknights")
    assert backend.secure_mode_enabled()
    seen: list[list[str]] = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    patch_everywhere(
        monkeypatch, winegame, "stream", lambda argv, env, **kw: seen.append(argv) or 0
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    assert backend.launch() == 0
    assert Path(seen[0][0]).name == "bwrap"
    assert seen[0][-1] == str(exe)
