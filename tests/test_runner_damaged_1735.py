# SPDX-License-Identifier: GPL-3.0-or-later
"""Damaged local pins are replaced once; verified future layouts are returned."""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from dataclasses import replace

import pytest
from runner_layout import make_runner

from nadare.core import runners

TAG = "dwproton-11.0-17"
NAME = TAG + "-x86_64"
CORE = ("ntdll.dll", "kernel32.dll", "kernelbase.dll")


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


@pytest.fixture
def archive(tmp_path, monkeypatch):
    def prepare(*, lib="lib", future=False):
        tree = make_runner(tmp_path / "source" / NAME, lib=lib)
        if future:
            (tree / "files" / "bin" / "wine").unlink()
        tar = tmp_path / (NAME + ".tar.xz")
        with tarfile.open(tar, "w:xz") as out:
            out.add(tree, arcname=NAME)
        body = tar.read_bytes()
        spec = replace(
            runners.RUNNERS["dwproton"],
            tested=(TAG, hashlib.sha256(body).hexdigest()),
            pinned_asset=(tar.name, len(body)),
        )
        monkeypatch.setitem(runners.RUNNERS, "dwproton", spec)
        calls = []

        def open_archive(url, **kw):
            calls.append(url)
            response = io.BytesIO(body)
            response.getcode = lambda: 200
            response.headers = {"Content-Length": str(len(body))}
            return response

        monkeypatch.setattr(runners.download, "http_open", open_archive)
        return calls

    return prepare


@pytest.mark.parametrize("lib", ["lib", "lib64"])
@pytest.mark.parametrize("missing", ["wine", *CORE])
def test_damaged_pin_is_downloaded_and_safely_replaced(root, archive, lib, missing):
    pinned = make_runner(root / NAME, lib=lib)
    file = pinned / "files" / "bin" / "wine" if missing == "wine" else (
        pinned / "files" / lib / "wine" / "x86_64-windows" / missing
    )
    file.unlink()
    (pinned / "old-marker").write_text("damaged tree")
    calls = archive(lib=lib)
    events = []
    assert runners.installed_pinned() is None
    assert runners.ensure_pinned(emit=events.append) == pinned
    assert file.is_file()
    assert not (pinned / "old-marker").exists()
    assert list(root.iterdir()) == [pinned]
    assert runners.ensure_pinned() == pinned
    assert len(calls) == 1
    assert any(e.message == "checksum ok (pinned sha256)" for e in events)


@pytest.mark.parametrize("lib", ["lib", "lib64"])
def test_good_pin_needs_no_download_or_version_file(root, monkeypatch, lib):
    pinned = make_runner(root / NAME, lib=lib)
    monkeypatch.setattr(runners, "install", lambda *a, **kw: pytest.fail("downloaded good pin"))
    events = []
    assert runners.installed_pinned() == pinned
    assert runners.ensure_pinned(emit=events.append) == pinned
    assert not events


def test_verified_future_layout_is_returned_once_with_warning(root, archive, caplog):
    calls = archive(future=True)
    events = []
    with caplog.at_level(logging.WARNING, logger="nadare.runners"):
        result = runners.ensure_pinned(emit=events.append)
    assert result == root / NAME
    assert (result / "proton").is_file()
    assert not (result / "files" / "bin" / "wine").exists()
    assert len(calls) == 1
    assert any(e.message == "checksum ok (pinned sha256)" for e in events)
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert NAME in warnings[0] and "files/bin/wine" in warnings[0]
    assert "using it anyway" in warnings[0]
    assert any(e.kind == "log" and "warning" in e.message.lower() for e in events)
    # The next launch uses the accepted copy: no download on every launch, no new warning.
    assert runners.ensure_pinned() == result
    assert len(calls) == 1


def test_damage_is_explained_in_the_run_log_before_download(root, monkeypatch):
    pinned = make_runner(root / NAME)
    windows = pinned / "files" / "lib" / "wine" / "x86_64-windows"
    for dll in CORE:
        (windows / dll).unlink()
    events = []

    def install(family, *, latest, emit, cancel):
        assert family == "dwproton" and latest is False
        assert [e.kind for e in events] == ["log"]
        assert events[0].message == (
            f"runner {NAME} is missing files {', '.join(CORE)}; downloading it again"
        )
        return make_runner(pinned)

    monkeypatch.setattr(runners, "install", install)
    assert runners.ensure_pinned(emit=events.append) == pinned
    assert len(events) == 1


def test_accepted_layout_still_notices_new_damage(root, archive):
    archive(future=True)
    result = runners.ensure_pinned()
    assert runners.installed_pinned() == result
    (result / "files" / "lib" / "wine" / "x86_64-windows" / "ntdll.dll").unlink()
    assert runners.installed_pinned() is None
