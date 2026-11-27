# SPDX-License-Identifier: GPL-3.0-or-later
"""The stages that ran silently after a download reached 100% now say what they do.

Two of them were reported in b29: the check of a large ranged download (concatenating
the parts and hashing the whole file) and a first launch into a prefix that did not
exist yet (Proton creates it silently).
"""

from __future__ import annotations

import contextlib
import threading

from _backend_helpers import make_context
from _httpserver import Server

from nadare.backends.direct_bpsr import BpsrBackend
from nadare.backends.winegame_parts import launching
from nadare.core import download as dl
from nadare.core.events import Event


def test_a_ranged_download_names_the_check_before_it_hashes(tmp_path, monkeypatch):
    data = bytes(range(256)) * 1024
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 1024)
    monkeypatch.setattr(dl.urllib.request, "getproxies", lambda: {})
    server = Server({"/file": data})
    events: list[Event] = []
    try:
        dl.download(
            server.url + "/file",
            tmp_path / "file",
            expected_size=len(data),
            range_workers=4,
            emit=events.append,
        )
    finally:
        server.close()

    last_progress = max(
        index
        for index, event in enumerate(events)
        if event.kind == "progress" and (event.progress or 0) >= 1.0
    )
    step = next(
        event
        for event in events[last_progress + 1 :]
        if event.kind == "step"
    )
    assert "check" in step.message.lower(), step.message
    # The hashes are computed after the check; its "verified" log line comes later.
    verified = next(
        index
        for index, event in enumerate(events)
        if event.kind == "log" and "verified" in (event.message or "")
    )
    assert events.index(step) < verified


def test_a_first_launch_names_the_prefix_before_it_creates_it(tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    target = game_dir / "Game.exe"
    target.write_bytes(b"MZ")
    prefix = tmp_path / "prefix"
    assert not prefix.exists()
    ctx = make_context(
        tmp_path, "direct-bpsr", {"prefix": str(prefix), "install_dir": str(game_dir)}
    )
    events: list[Event] = []
    ctx.emit = events.append
    backend = BpsrBackend(ctx)

    monkeypatch.setattr(backend, "direct_target", lambda: target)
    monkeypatch.setattr(backend, "_run_vcredist", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_direct_argv", lambda target: [str(target)])
    monkeypatch.setattr(
        backend,
        "_prefix_command",
        lambda args, env=None, **kwargs: (["proton-run", *args], {}),
    )
    monkeypatch.setattr(backend, "_secure_wrap", lambda argv, env: (argv, env))
    monkeypatch.setattr(backend, "_record_polite_stop", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_runtime_note", lambda: "")
    monkeypatch.setattr(backend, "_runner_facts", lambda: {})
    monkeypatch.setattr(backend, "_log_launch_details", lambda env: None)
    monkeypatch.setattr(backend, "_run_lock", contextlib.nullcontext)
    closed = threading.Event()
    closed.set()
    monkeypatch.setattr(backend, "_game_process_watch", lambda: contextlib.nullcontext(closed))
    monkeypatch.setattr(backend, "_begin_game_output", lambda: None)
    monkeypatch.setattr(backend, "_end_game_output", lambda: None)
    monkeypatch.setattr(backend, "_stop_link_relay", lambda: None)
    monkeypatch.setattr(
        type(backend),
        "_renderer_probe",
        lambda self: _Probe(),
    )
    monkeypatch.setattr(launching.graphics, "effective_renderer", lambda profile: (None, ""))
    monkeypatch.setattr(launching, "stream", lambda *args, **kwargs: 0)

    assert backend._launch_direct() == 0
    steps = [event.message for event in events if event.kind == "step"]
    assert steps[0] == "Preparing the Wine prefix…"
    assert steps[1].endswith(": launching Game.exe")
    assert not prefix.exists()  # nothing was actually created by this launch


class _Probe:
    result = ""

    def start(self):
        return self

    def stop(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass
