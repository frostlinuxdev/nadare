# SPDX-License-Identifier: GPL-3.0-or-later
"""W5-F3: the Kuro tidy asks "running?" before OperationLock, and never again inside it."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch

EXTRA = {kuropatch.EXTRA_KEY: {"root": "https://cdn.example/", "packs": {"": []}}}


def test_the_tidy_rechecks_a_game_that_started_between_check_and_lock(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "pfx")})
    backend = direct.WutheringWavesBackend(ctx)
    root = backend.install_dir()
    (root / "Client").mkdir(parents=True)
    dead = root / "Client" / "dead.pak"
    dead.write_bytes(b"retired")
    backend._write_version_file("3.8.0", "wuwa")
    backend._load_manifest = lambda: dd.Plan(game_tag="wuwa", version="3.8.0", extra=EXTRA)
    backend.info = lambda text: None
    monkeypatch.setattr(
        kuropatch,
        "retired_scan",
        lambda *a: kuropatch.RetiredScan(("Client/dead.pak",), ()),
    )

    # First answer is the pre-lock check; the game starts in the window between
    # that check and taking the lock, so the second answer (inside the lock) is
    # "running".  The tidy must then leave the files alone.
    answers = iter([False, True])
    calls: list[bool] = []

    def running() -> bool:
        value = next(answers, True)
        calls.append(value)
        return value

    monkeypatch.setattr(backend, "_game_running", running)
    backend._tidy_retired_once()

    assert dead.exists(), (
        f"the tidy deleted {dead} although the game was running by the time it "
        f"held the lock; _game_running was asked {len(calls)} time(s): {calls}"
    )
