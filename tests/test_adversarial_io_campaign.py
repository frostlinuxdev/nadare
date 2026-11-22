# SPDX-License-Identifier: GPL-3.0-or-later
"""Injected storage failures: preserve the last config and original game bytes."""

from __future__ import annotations

import errno
import json

import pytest

from nadare.core import config as config_module
from nadare.core import relocate
from nadare.core.config import TOP_DEFAULTS, Config

ERRORS = [errno.ENOSPC, errno.EIO, errno.EROFS]


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("boundary", ["serialize", "file_sync", "publish", "directory_sync"])
@pytest.mark.parametrize("title", ["Game", "空 白", "A" * 64])
def test_config_write_failure_is_recoverable(tmp_path, monkeypatch, error, boundary, title):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("g", backend="wine", title=title)
    cfg.save()
    original = json.loads(path.read_text())
    cfg.data["language"] = "fr"
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError(error, "injected storage failure")

    with monkeypatch.context() as patch:
        if boundary == "serialize":
            patch.setattr(config_module.json, "dump", fail)
        elif boundary == "file_sync":
            patch.setattr(config_module.os, "fsync", fail)
        elif boundary == "publish":
            patch.setattr(config_module.os, "replace", fail)
        else:
            patch.setattr(config_module, "_sync_dir", fail)
        with pytest.raises(OSError):
            cfg.save()
    assert calls, "fault was not reached"
    raw = json.loads(path.read_text())
    assert raw["profiles"] == original["profiles"]
    assert raw["language"] == ("fr" if boundary == "directory_sync" else TOP_DEFAULTS["language"])
    for _ in range(2):
        recovered = Config.load(path)
        assert recovered.profiles["g"]["title"] == title
        recovered.data["language"] = "de"
        recovered.save()
    assert Config.load(path).data["language"] == "de"
    assert not list(tmp_path.glob(".config-*.tmp"))


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("boundary", ["first_chunk", "last_chunk", "publish", "cleanup_sync"])
def test_move_write_failure_keeps_original_and_retry_works(tmp_path, monkeypatch, error, boundary):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    body = bytes(range(256)) * 64
    (source / "game.bin").write_bytes(body)
    monkeypatch.setattr(relocate, "_same_device", lambda *args: False)
    monkeypatch.setattr(relocate, "CHUNK", 1024)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError(error, "injected storage failure")

    with monkeypatch.context() as patch:
        progress = None
        if boundary in ("first_chunk", "last_chunk"):
            threshold = 1024 if boundary == "first_chunk" else len(body)

            def progress(done, total):
                if done >= threshold:
                    fail()
        elif boundary == "publish":
            patch.setattr(relocate.os, "rename", fail)
        if boundary == "cleanup_sync":
            relocate.move_tree(source, destination)
            patch.setattr(relocate.os, "sync", fail)
            result = relocate.remove_original(source, destination)
            assert not result.removed
            assert (destination / "game.bin").read_bytes() == body
        else:
            with pytest.raises(relocate.RelocateError):
                relocate.move_tree(source, destination, progress=progress)
            assert not destination.exists()
            assert not destination.with_name(destination.name + relocate.PARTIAL_SUFFIX).exists()
    assert calls, "fault was not reached"
    assert (source / "game.bin").read_bytes() == body
    if not destination.exists():
        relocate.move_tree(source, destination)
    assert (destination / "game.bin").read_bytes() == body
    # Verify retryability without syncing the entire host for each test.
    monkeypatch.setattr(relocate.os, "sync", lambda: None)
    assert relocate.remove_original(source, destination).removed
    assert not source.exists()


@pytest.mark.parametrize("error", ERRORS)
def test_migration_then_failed_config_publish_is_recoverable(tmp_path, monkeypatch, error):
    from nadare.core import paths

    old = paths.profiles_dir() / "old" / "name"
    (old / "prefix").mkdir(parents=True)
    (old / "prefix" / "save.dat").write_bytes(b"precious-save")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "profiles": {"old/name": {"backend": "wine", "prefix": str(old / "prefix")}},
                "active_profile": "old/name",
            }
        )
    )
    cfg = Config.load(config)  # moves directory before the config can be published
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError(error, "injected failure after migration")

    with monkeypatch.context() as patch:
        patch.setattr(config_module.os, "replace", fail)
        with pytest.raises(OSError):
            cfg.save()
    assert calls
    assert "old/name" in json.loads(config.read_text())["profiles"]
    for _ in range(2):
        cfg = Config.load(config)
        assert cfg.data["active_profile"] == "old-name"
        prefix = paths.profiles_dir() / "old-name" / "prefix"
        assert cfg.profiles["old-name"]["prefix"] == str(prefix)
        assert (prefix / "save.dat").read_bytes() == b"precious-save"
        cfg.save()
