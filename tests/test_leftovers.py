# SPDX-License-Identifier: GPL-3.0-or-later
"""Successful sync cleanup is limited to Nadare-owned leftovers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from _httpserver import Server
from test_sophon import Build, plan_for

from nadare.core import directdl as dd
from nadare.core import sophon
from nadare.core.events import Cancel, Cancelled


@pytest.fixture
def sophon_servers():
    made: list[Server] = []

    def make(build: Build) -> Server:
        server = build.serve()
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


@pytest.fixture
def direct_server():
    made: list[Server] = []

    def make(objects: dict[str, bytes]) -> Server:
        server = Server(objects)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def ledger_path(root: Path) -> Path:
    return root / ".nadare" / "sophon-game.files"


def test_sophon_prunes_only_ledger_owned_files_dropped_from_manifest(sophon_servers, tmp_path):
    root = tmp_path / "install"
    (root / "obsolete" / "nested").mkdir(parents=True)
    (root / "obsolete" / "nested" / "old.bin").write_bytes(b"old game data")
    (root / "obsolete" / "save.dat").write_bytes(b"user save")
    (root / "old-root.bin").write_bytes(b"old file")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("obsolete/nested/old.bin\nold-root.bin\n", encoding="utf-8")

    build = Build({"current.bin": b"new game data"})
    sophon_servers(build)
    dd.sync(plan_for(build), root)

    assert not (root / "obsolete" / "nested" / "old.bin").exists()
    assert not (root / "old-root.bin").exists()
    assert (root / "obsolete" / "save.dat").read_bytes() == b"user save"
    assert not (root / "obsolete" / "nested").exists()
    assert state.read_text(encoding="utf-8") == "current.bin\n"


def test_sophon_cleanup_keeps_path_owned_by_another_package(sophon_servers, tmp_path):
    root = tmp_path / "install"
    root.mkdir()

    package_a_initial = Build({"shared.bin": b"package A data"})
    sophon_servers(package_a_initial)
    sophon.sync_package(package_a_initial.package("package-a"), root)

    package_b = Build({"shared.bin": b"package B data"})
    sophon_servers(package_b)
    sophon.sync_package(package_b.package("package-b"), root)
    assert (root / "shared.bin").read_bytes() == b"package B data"

    package_a_updated = Build({})
    sophon_servers(package_a_updated)
    sophon.sync_package(package_a_updated.package("package-a"), root)

    assert (root / "shared.bin").read_bytes() == b"package B data"
    assert (root / ".nadare" / "sophon-package-a.files").read_text(encoding="utf-8") == ""
    assert (root / ".nadare" / "sophon-package-b.files").read_text(encoding="utf-8") == (
        "shared.bin\n"
    )


def test_sophon_first_sync_without_ledger_preserves_untracked_files(sophon_servers, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "user-config.json").write_text("keep me", encoding="utf-8")
    (root / "mods").mkdir()
    (root / "mods" / "patch.bin").write_bytes(b"user mod")

    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    dd.sync(plan_for(build), root)

    assert (root / "user-config.json").read_text(encoding="utf-8") == "keep me"
    assert (root / "mods" / "patch.bin").read_bytes() == b"user mod"
    assert (root / "game.bin").read_bytes() == b"game data"
    assert ledger_path(root).read_text(encoding="utf-8") == "game.bin\n"


@pytest.mark.parametrize("bad_path", ["../outside.bin", "/outside.bin"])
def test_sophon_rejects_unsafe_paths_in_old_ledger(sophon_servers, tmp_path, bad_path):
    root = tmp_path / "install"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must survive")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text(f"{bad_path}\n", encoding="utf-8")

    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    with pytest.raises(dd.DirectError, match="unsafe"):
        dd.sync(plan_for(build), root)

    assert outside.read_bytes() == b"must survive"
    assert state.read_text(encoding="utf-8") == f"{bad_path}\n"
    assert not (root / "game.bin").exists()


def test_sophon_cleanup_never_follows_symlinked_ledger_parent(sophon_servers, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.bin").write_bytes(b"user data")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("linked/victim.bin\n", encoding="utf-8")

    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    dd.sync(plan_for(build), root)

    assert (outside / "victim.bin").read_bytes() == b"user data"
    assert (root / "linked").is_symlink()


def test_sophon_failed_sync_keeps_old_ledger_and_dropped_files(
    sophon_servers, tmp_path, monkeypatch
):
    root = tmp_path / "install"
    root.mkdir()
    (root / "old.bin").write_bytes(b"still tracked")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("old.bin\n", encoding="utf-8")
    old_ledger = state.read_bytes()

    build = Build({"game.bin": b"game data"})
    sophon_servers(build)

    def fail_fetch(*args, **kwargs):
        raise sophon.SophonError("injected chunk failure")

    monkeypatch.setattr(sophon, "_fetch_chunk", fail_fetch)
    with pytest.raises(dd.DirectError, match="injected chunk failure"):
        dd.sync(plan_for(build), root)

    assert (root / "old.bin").read_bytes() == b"still tracked"
    assert state.read_bytes() == old_ledger


def test_sophon_cancelled_sync_keeps_old_ledger_and_dropped_files(
    sophon_servers, tmp_path, monkeypatch
):
    root = tmp_path / "install"
    root.mkdir()
    (root / "old.bin").write_bytes(b"still tracked")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("old.bin\n", encoding="utf-8")
    old_ledger = state.read_bytes()

    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    cancel = Cancel()
    real_fetch = sophon._fetch_chunk

    def cancel_after_fetch(*args, **kwargs):
        data = real_fetch(*args, **kwargs)
        cancel.cancel()
        return data

    monkeypatch.setattr(sophon, "_fetch_chunk", cancel_after_fetch)
    with pytest.raises((Cancelled, dd.DirectError)):
        dd.sync(plan_for(build), root, cancel=cancel)

    assert (root / "old.bin").read_bytes() == b"still tracked"
    assert state.read_bytes() == old_ledger


def test_successful_direct_sync_removes_part_files_only_inside_install(direct_server, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "stale.part").write_bytes(b"stale")
    (root / "nested").mkdir()
    (root / "nested" / "stale.part").write_bytes(b"stale")
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"outside")
    body = b"valid download"
    active_part = b"a manifest file whose name ends in .part"
    server = direct_server({"/game.bin": body, "/active.part": active_part})
    plan = dd.Plan(
        "T",
        "1",
        files=(
            dd.RemoteFile(
                "game.bin",
                server.url + "/game.bin",
                len(body),
                md5=hashlib.md5(body).hexdigest(),
            ),
            dd.RemoteFile(
                "active.part",
                server.url + "/active.part",
                len(active_part),
                md5=hashlib.md5(active_part).hexdigest(),
            ),
        ),
    )

    dd.sync(plan, root)

    assert (root / "game.bin").read_bytes() == body
    assert (root / "active.part").read_bytes() == active_part
    assert not (root / "stale.part").exists()
    assert not (root / "nested" / "stale.part").exists()
    assert outside.read_bytes() == b"outside"


def test_successful_sophon_sync_keeps_manifest_part_files(sophon_servers, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "stale.part").write_bytes(b"stale")
    active_part = b"a Sophon manifest file whose name ends in .part"
    build = Build({"active.part": active_part})
    sophon_servers(build)

    dd.sync(plan_for(build), root)

    assert (root / "active.part").read_bytes() == active_part
    assert not (root / "stale.part").exists()


def test_failed_direct_sync_keeps_part_files(direct_server, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    stale = root / "stale.part"
    stale.write_bytes(b"keep after failure")
    server = direct_server({"/game.bin": b"wrong"})
    plan = dd.Plan(
        "T",
        "1",
        files=(dd.RemoteFile("game.bin", server.url + "/game.bin", 5, md5="0" * 32),),
    )

    with pytest.raises(dd.DirectError):
        dd.sync(plan, root)

    assert stale.read_bytes() == b"keep after failure"


def test_sophon_ledger_write_failure_does_not_remove_old_files(
    sophon_servers, tmp_path, monkeypatch
):
    root = tmp_path / "install"
    root.mkdir()
    (root / "old.bin").write_bytes(b"still tracked")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("old.bin\n", encoding="utf-8")
    old_ledger = state.read_bytes()
    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    real_write = sophon._atomic_write_text

    def fail_ledger(path, text):
        if path == state:
            raise sophon.SophonError("injected ledger write failure")
        real_write(path, text)

    monkeypatch.setattr(sophon, "_atomic_write_text", fail_ledger)
    with pytest.raises((sophon.SophonError, dd.DirectError), match="ledger write failure"):
        dd.sync(plan_for(build), root)

    assert (root / "old.bin").read_bytes() == b"still tracked"
    assert state.read_bytes() == old_ledger
    assert not sophon.is_done(build.package(), root)


@pytest.mark.parametrize("symlink_state_dir", [False, True])
def test_sophon_refuses_symlinked_ledger_storage(sophon_servers, tmp_path, symlink_state_dir):
    root = tmp_path / "install"
    root.mkdir()
    (root / "old.bin").write_bytes(b"still tracked")
    outside = tmp_path / "outside-state"
    outside.mkdir()
    outside_ledger = outside / "sophon-game.files"
    outside_ledger.write_text("old.bin\n", encoding="utf-8")
    local_state = root / ".nadare"
    if symlink_state_dir:
        local_state.symlink_to(outside, target_is_directory=True)
    else:
        local_state.mkdir()
        ledger_path(root).symlink_to(outside_ledger)
    build = Build({"game.bin": b"game data"})
    sophon_servers(build)

    with pytest.raises(dd.DirectError, match="symlink"):
        dd.sync(plan_for(build), root)

    assert (root / "old.bin").read_bytes() == b"still tracked"
    assert outside_ledger.read_text(encoding="utf-8") == "old.bin\n"
    assert not (root / "game.bin").exists()


def test_sophon_marker_write_failure_restores_old_ledger(sophon_servers, tmp_path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    (root / "old.bin").write_bytes(b"still tracked")
    state = ledger_path(root)
    state.parent.mkdir()
    state.write_text("old.bin\n", encoding="utf-8")
    old_ledger = state.read_bytes()
    marker = root / ".nadare" / "sophon-game.done"
    marker.write_text("old-manifest", encoding="utf-8")
    build = Build({"game.bin": b"game data"})
    sophon_servers(build)
    real_write = sophon._atomic_write_text

    def fail_marker(path, text):
        if path == marker:
            raise sophon.SophonError("injected marker write failure")
        real_write(path, text)

    monkeypatch.setattr(sophon, "_atomic_write_text", fail_marker)
    with pytest.raises(dd.DirectError, match="marker write failure"):
        dd.sync(plan_for(build), root)

    assert (root / "old.bin").read_bytes() == b"still tracked"
    assert state.read_bytes() == old_ledger
    assert marker.read_text(encoding="utf-8") == "old-manifest"
