# SPDX-License-Identifier: GPL-3.0-or-later
"""SIGKILL during real config, migration, download and move operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _adversarial_crash import CHECKPOINT, kill_at_checkpoint
from test_download import FakeResponse

from nadare.core import download, paths, relocate
from nadare.core.config import TOP_DEFAULTS, Config


@pytest.mark.parametrize("boundary", ["before_replace", "after_replace"])
def test_killed_config_save_is_atomic_and_can_save_again(tmp_path, boundary):
    config = tmp_path / "config.json"
    cfg = Config.load(config)
    cfg.add_profile("g", backend="wine", title="Keep")
    cfg.save()
    script = f"""
import os
from pathlib import Path
from nadare.core.config import Config
cfg = Config.load(Path({str(config)!r}))
cfg.data['language'] = 'fr'
real = os.replace
def replace(src, dst):
    if {boundary!r} == 'before_replace':
        {CHECKPOINT}
    real(src, dst)
    if {boundary!r} == 'after_replace':
        {CHECKPOINT}
os.replace = replace
cfg.save()
"""
    kill_at_checkpoint(script, tmp_path)
    raw = json.loads(config.read_text())
    assert raw["profiles"]["g"]["title"] == "Keep"
    assert raw["language"] == (TOP_DEFAULTS["language"] if boundary == "before_replace" else "fr")
    for _ in range(2):
        cfg = Config.load(config)
        cfg.data["language"] = "de"
        cfg.save()
    assert not list(tmp_path.glob(".config-*.tmp"))


def test_killed_unsafe_profile_migration_recovers_after_directory_rename(tmp_path):
    old = paths.profiles_dir() / "old" / "name"
    (old / "prefix").mkdir(parents=True)
    (old / "prefix" / "save.dat").write_bytes(b"save")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "profiles": {"old/name": {"backend": "wine", "prefix": str(old / "prefix")}},
                "active_profile": "old/name",
            }
        )
    )
    script = f"""
from pathlib import Path
from nadare.core.config import Config
real = Path.rename
def rename(self, target):
    result = real(self, target)
    {CHECKPOINT}
    return result
Path.rename = rename
Config.load(Path({str(config)!r}))
"""
    kill_at_checkpoint(script, tmp_path)
    for _ in range(2):
        cfg = Config.load(config)
        assert cfg.data["active_profile"] == "old-name"
        assert (Path(cfg.profiles["old-name"]["prefix"]) / "save.dat").read_bytes() == b"save"
        cfg.save()


@pytest.mark.parametrize("chunk_number", [1, 3, 7])
def test_killed_download_never_promotes_partial_and_resumes(tmp_path, monkeypatch, chunk_number):
    body = bytes(range(256)) * 64
    dest = tmp_path / "game.bin"
    digest = hashlib.sha256(body).hexdigest()
    script = f"""
from pathlib import Path
from test_download import FakeResponse
from nadare.core import download as dl
body = bytes(range(256)) * 64
dl.http_open = lambda *a, **k: FakeResponse(body, headers={{'Content-Length': str(len(body))}})
def event(e):
    if e.kind == 'progress' and e.data.get('done', 0) >= {chunk_number * 1024}:
        {CHECKPOINT}
dl.download('https://example.invalid/game', Path({str(dest)!r}), expected_size=len(body),
            expected_sha256={digest!r}, chunk=1024, emit=event)
"""
    kill_at_checkpoint(script, tmp_path)
    assert not dest.exists()
    part = dest.with_name(dest.name + ".part")
    assert part.exists() and part.stat().st_size < len(body)
    calls = []

    def open_response(url, *, headers=None, **kwargs):
        headers = headers or {}
        offset = int(headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        calls.append(offset)
        head = {"Content-Length": str(len(body) - offset)}
        if offset:
            head["Content-Range"] = f"bytes {offset}-{len(body) - 1}/{len(body)}"
        return FakeResponse(body[offset:], status=206 if offset else 200, headers=head)

    monkeypatch.setattr(download, "http_open", open_response)
    download.download(
        "https://example.invalid/game",
        dest,
        expected_size=len(body),
        expected_sha256=digest,
        chunk=1024,
    )
    assert calls and dest.read_bytes() == body
    assert not part.exists()


@pytest.mark.parametrize("boundary", ["journal_saved", "mid_copy", "moved", "profile_saved"])
@pytest.mark.parametrize("cross_disk", [False, True])
def test_killed_move_recovers_twice_without_losing_game(tmp_path, boundary, cross_disk):
    if boundary == "mid_copy" and not cross_disk:
        pytest.skip("same-filesystem rename has no copy checkpoint")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    body = bytes(range(256)) * 32
    (old / "game.bin").write_bytes(body)
    config = tmp_path / "config.json"
    cfg = Config.load(config)
    cfg.add_profile("g", backend="wine", prefix=str(old))
    cfg.save()
    script = f"""
from pathlib import Path
from nadare.core import relocate
from nadare.core.config import Config
old, new = Path({str(old)!r}), Path({str(new)!r})
cfg = Config.load(Path({str(config)!r}))
p = cfg.profiles['g']
relocate.begin_move(p, old, new)
cfg.save()
def checkpoint():
    {CHECKPOINT}
if {boundary!r} == 'journal_saved': checkpoint()
relocate._same_device = lambda *args: {not cross_disk!r}
relocate.CHUNK = 1024
def progress(done, total):
    if {boundary!r} == 'mid_copy' and done >= 1024: checkpoint()
moved = relocate.move_tree(old, new, progress=progress,
                           journal=p[relocate.MOVE_IN_PROGRESS], checkpoint=cfg.save)
if {boundary!r} == 'moved': checkpoint()
p['prefix'] = str(new)
relocate.end_move(p)
cfg.save()
if {boundary!r} == 'profile_saved': checkpoint()
"""
    kill_at_checkpoint(script, tmp_path)
    for _ in range(2):
        cfg = Config.load(config)
        relocate.finish_interrupted_moves(cfg.profiles)
        cfg.save()
        current = Path(cfg.profiles["g"]["prefix"])
        assert (current / "game.bin").read_bytes() == body
        assert relocate.MOVE_IN_PROGRESS not in cfg.profiles["g"]
    assert any((folder / "game.bin").exists() for folder in (old, new))


@pytest.mark.parametrize("through_process_run", [False, True])
def test_killed_launcher_does_not_orphan_parent_bound_child(tmp_path, through_process_run):
    import os
    import signal
    import time

    pidfile = tmp_path / "game.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "print('stub-ready', flush=True); time.sleep(60)"
    )
    script = f"""
import subprocess,sys,time
from pathlib import Path
from nadare.core import gamesandbox, process
argv = gamesandbox.die_with_parent([sys.executable, '-c', {child_code!r}])
if {through_process_run!r}:
    def emit(event):
        if event.kind == 'log' and event.message == 'stub-ready':
            {CHECKPOINT}
    process.run(argv, emit=emit)
else:
    subprocess.Popen(argv, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not Path({str(pidfile)!r}).exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    {CHECKPOINT}
"""
    pid = None
    try:
        kill_at_checkpoint(script, tmp_path)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        alive = True
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                alive = state != "Z"
            except (FileNotFoundError, ProcessLookupError):  # gone between open and read
                alive = False
            if not alive:
                break
            time.sleep(0.01)
        assert not alive, "parent-bound game stub survived SIGKILL of its launcher"
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
