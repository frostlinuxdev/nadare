# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1364 phase 4: one note when a runner's DXVK or VKD3D build differs.

Proton trees add ``files/lib/wine/dxvk/version`` and
``files/lib/wine/vkd3d-proton/version``.  The first Play records them; a later
Play on a runner that ships another build says so once, and then stays quiet
until the next change.  A runner without those files is left alone.
"""

from __future__ import annotations

from pathlib import Path

from _backend_helpers import make_context

from nadare.backends.winegame import WineGameBackend
from nadare.core import shadercache

DXVK_NOTE = "New DXVK version: the game may stutter while its shaders rebuild."
VKD3D_NOTE = "New VKD3D version: the game may stutter while its shaders rebuild."
BOTH_NOTE = "New DXVK and VKD3D versions: the game may stutter while its shaders rebuild."


def _runner(tmp_path: Path, name: str, *, dxvk: str | None, vkd3d: str | None) -> Path:
    runner = tmp_path / name
    (runner / "files" / "lib" / "wine").mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    for folder, value in (("dxvk", dxvk), ("vkd3d-proton", vkd3d)):
        if value is None:
            continue
        target = runner / "files" / "lib" / "wine" / folder
        target.mkdir()
        (target / "version").write_text(value + "\n")
    return runner


def _note(profile: str, runner: Path, messages: list[str], data: Path) -> None:
    shadercache.note_runner_change(profile, runner, messages.append, data_dir=data)


def test_versions_are_read_from_the_runner_tree(tmp_path):
    runner = _runner(
        tmp_path,
        "GE-Proton11-7",
        dxvk=" 601930949d111edbbcf9dd463948426d9f8f6ddd dxvk (v3.1-19-g601930949)",
        vkd3d=" af89350cc2eacd9da2293fbae96bd9ab4987c9bb vkd3d-proton (vkd3d-1.1-5604)",
    )

    assert shadercache.runner_versions(runner) == {
        "DXVK": "601930949d111edbbcf9dd463948426d9f8f6ddd dxvk (v3.1-19-g601930949)",
        "VKD3D": "af89350cc2eacd9da2293fbae96bd9ab4987c9bb vkd3d-proton (vkd3d-1.1-5604)",
    }


def test_a_runner_without_version_files_is_skipped(tmp_path):
    runner = _runner(tmp_path, "plain", dxvk=None, vkd3d=None)
    messages: list[str] = []
    data = tmp_path / "profile"

    assert shadercache.runner_versions(runner) == {}
    _note("g", runner, messages, data)
    assert messages == []
    assert not (data / shadercache.VERSIONS_FILE).exists()


def test_the_first_play_records_and_says_nothing(tmp_path):
    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    messages: list[str] = []
    data = tmp_path / "profile"

    _note("g", runner, messages, data)

    assert messages == []
    assert (data / shadercache.VERSIONS_FILE).is_file()


def test_the_same_runner_stays_quiet_on_the_next_play(tmp_path):
    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", runner, messages, data)

    _note("g", runner, messages, data)

    assert messages == []


def test_a_new_dxvk_build_is_named_once(tmp_path):
    old = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    new = _runner(tmp_path, "r2", dxvk="dxvk 2", vkd3d="vkd3d 1")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", old, messages, data)

    _note("g", new, messages, data)
    _note("g", new, messages, data)

    assert messages == [DXVK_NOTE]


def test_a_new_vkd3d_build_is_named(tmp_path):
    old = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    new = _runner(tmp_path, "r2", dxvk="dxvk 1", vkd3d="vkd3d 2")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", old, messages, data)

    _note("g", new, messages, data)

    assert messages == [VKD3D_NOTE]


def test_both_changes_are_one_line(tmp_path):
    old = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    new = _runner(tmp_path, "r2", dxvk="dxvk 2", vkd3d="vkd3d 2")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", old, messages, data)

    _note("g", new, messages, data)

    assert messages == [BOTH_NOTE]


def test_a_missing_version_file_does_not_warn_or_forget_the_old_one(tmp_path):
    old = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    partial = _runner(tmp_path, "r2", dxvk="dxvk 1", vkd3d=None)
    changed = _runner(tmp_path, "r3", dxvk="dxvk 1", vkd3d="vkd3d 2")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", old, messages, data)

    _note("g", partial, messages, data)
    _note("g", changed, messages, data)

    assert messages == [VKD3D_NOTE]


def test_a_lost_record_means_a_new_baseline_not_a_false_alarm(tmp_path):
    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    messages: list[str] = []
    data = tmp_path / "profile"
    _note("g", runner, messages, data)
    (data / shadercache.VERSIONS_FILE).unlink()

    _note("g", runner, messages, data)

    assert messages == []


def _backend(tmp_path, monkeypatch, runner: Path) -> tuple[WineGameBackend, list[str]]:
    ctx = make_context(
        tmp_path,
        "wine",
        {"prefix": str(tmp_path / "prefix"), "wine_binary": "/bin/true"},
    )
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (runner, "proton"))
    monkeypatch.setattr("nadare.core.protoncmd.proton_argv", lambda args, **kw: args)
    monkeypatch.setattr("nadare.core.protoncmd.note_prefix_upgrade", lambda *a, **kw: None)
    monkeypatch.setattr("nadare.core.protoncmd.play_speedups", lambda *a, **kw: None)
    messages: list[str] = []
    monkeypatch.setattr(backend, "info", messages.append)
    return backend, messages


def test_a_play_through_the_backend_says_it_once(tmp_path, monkeypatch):
    old = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    new = _runner(tmp_path, "r2", dxvk="dxvk 2", vkd3d="vkd3d 1")
    backend, messages = _backend(tmp_path, monkeypatch, old)

    backend._prefix_command(["game.exe"])
    backend._prefix_command(["game.exe"])
    assert DXVK_NOTE not in messages

    monkeypatch.setattr(backend, "resolve_runner", lambda: (new, "proton"))
    backend._prefix_command(["game.exe"])
    backend._prefix_command(["game.exe"])

    assert messages.count(DXVK_NOTE) == 1


def test_a_fifo_as_version_file_does_not_hang_the_launch(tmp_path):
    import os
    import threading

    runner = tmp_path / "runner"
    (runner / "files/lib/wine/dxvk").mkdir(parents=True)
    os.mkfifo(runner / "files/lib/wine/dxvk/version")
    result: list[dict[str, str]] = []
    worker = threading.Thread(target=lambda: result.append(shadercache.runner_versions(runner)))
    worker.daemon = True
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "reading a FIFO blocked the launch"
    assert result == [{}]


def test_a_fifo_as_the_saved_record_does_not_hang_the_launch(tmp_path):
    import os
    import threading

    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    folder = shadercache.paths.profiles_dir() / "g"
    folder.mkdir(parents=True)
    os.mkfifo(folder / shadercache.VERSIONS_FILE)
    done: list[bool] = []
    worker = threading.Thread(
        target=lambda: (
            shadercache.prepare("g", {}, lambda _m: None, runner=runner),
            done.append(True),
        )
    )
    worker.daemon = True
    worker.start()
    worker.join(5)

    assert not worker.is_alive(), "reading the saved record blocked the launch"
    assert done == [True]


def test_the_saved_record_is_not_read_through_a_link(tmp_path):
    import os

    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    data = tmp_path / "profile"
    data.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"DXVK": "dxvk 0"}\n')
    record = data / shadercache.VERSIONS_FILE
    os.symlink(outside, record)
    messages: list[str] = []

    _note("g", runner, messages, data)

    assert messages == []
    assert not record.is_symlink()


def test_an_oversized_saved_record_is_ignored(tmp_path):
    runner = _runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    data = tmp_path / "profile"
    data.mkdir()
    payload = '{"DXVK": "dxvk 0", "pad": "' + "a" * 70_000 + '"}'
    (data / shadercache.VERSIONS_FILE).write_text(payload)
    messages: list[str] = []

    _note("g", runner, messages, data)

    assert messages == []


def test_an_unexpected_error_in_the_note_does_not_stop_the_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(shadercache, "note_runner_change", boom)
    env: dict[str, str] = {}
    shadercache.prepare("sample", env, lambda _m: None, runner=tmp_path / "runner")
    assert "MESA_SHADER_CACHE_DIR" in env
