# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch cache hashing, outdated state, update flow, offline remove, CLI & worker (#1866)."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
import zipfile
import zlib
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare import app
from nadare.core import (
    backends,
    download,
    indexfetch,
    operation_policy,
    patches,
    patchops,
    patchspecs,
    process,
)
from nadare.core.config import Config
from nadare.gui.game_view_parts import busy
from nadare.gui.workers import BackendWorker

ORIGINAL = b"original japanese container\n" * 40
UPLOAD_V1 = b"english upload one\n" * 45
UPLOAD_V2 = b"english upload two (fixed typos)\n" * 52
NEW_GAME_BUILD = b"new japanese build container\n" * 48
KIND = "patchupdate-test"
PATCH_ID = "test-en"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_zip(path: Path, members: dict[str, bytes]) -> tuple[str, int]:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    raw = path.read_bytes()
    return _sha(raw), len(raw)


def _spec_for(
    zip_path: Path,
    after: bytes,
    *,
    before: bytes = ORIGINAL,
    version: str = "v1.0-100",
    url: str = "https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
) -> patches.PatchSpec:
    archive_sha, archive_size = _make_zip(zip_path, {"table_data.bytes": after})
    return patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version=version,
        url=url,
        archive_sha256=archive_sha,
        archive_size=archive_size,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(after),
                size=len(after),
                before_sha256=_sha(before),
                before_size=len(before),
            ),
        ),
    )


class FakeBackend(backends.GameBackend):
    kind = KIND

    def __init__(self, ctx, root: Path) -> None:
        super().__init__(ctx)
        self.root = root

    def install_dir(self) -> Path:
        return self.root

    def status(self):
        return backends.Status("installed")

    def install(self):
        pass

    def update(self):
        (self.root / "Data/table.r").write_bytes(NEW_GAME_BUILD)

    def repair(self):
        (self.root / "Data/table.r").write_bytes(ORIGINAL)

    def launch(self):
        return 0

    def stop(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    (root / "Data").mkdir(parents=True)
    (root / "Data/table.r").write_bytes(ORIGINAL)

    zip_v1 = tmp_path / "v1.zip"
    zip_v2 = tmp_path / "v2.zip"
    spec_v1 = _spec_for(zip_v1, UPLOAD_V1)
    spec_v2 = _spec_for(zip_v2, UPLOAD_V2)

    current_spec = {"spec": spec_v1}
    zips = {spec_v1.archive_sha256: zip_v1, spec_v2.archive_sha256: zip_v2}
    fetched: list[tuple[str, str | None]] = []
    refreshed: list[str] = []

    monkeypatch.setitem(patchspecs.SPECS, KIND, (spec_v1,))
    monkeypatch.setattr(
        patchspecs,
        "specs_for",
        lambda kind: (current_spec["spec"],) if kind == KIND else (),
    )

    def fake_refresh(kind: str):
        refreshed.append(kind)
        return (current_spec["spec"],) if kind == KIND else ()

    monkeypatch.setattr(patchspecs, "refresh", fake_refresh)

    def fake_download(url, dest, *, expected_sha256=None, **kwargs):
        fetched.append((url, expected_sha256))
        src = zips[expected_sha256]
        shutil.copyfile(src, dest)

    monkeypatch.setattr(download, "download", fake_download)
    monkeypatch.setitem(backends._REGISTRY, KIND, FakeBackend)

    ctx = make_context(tmp_path, KIND)
    events = []
    ctx.emit = events.append
    backend = FakeBackend(ctx, root)
    return {
        "backend": backend,
        "root": root,
        "spec_v1": spec_v1,
        "spec_v2": spec_v2,
        "current": current_spec,
        "zips": zips,
        "fetched": fetched,
        "refreshed": refreshed,
        "events": events,
        "tmp_path": tmp_path,
    }


def test_archive_cache_path_includes_hash_and_ignores_stale_old_hash_file(env):
    spec_v1, spec_v2 = env["spec_v1"], env["spec_v2"]
    path_v1 = patchops._archive_path(spec_v1)
    path_v2 = patchops._archive_path(spec_v2)
    assert path_v1.name == f"{spec_v1.id}-{spec_v1.version}-{spec_v1.archive_sha256[:16]}.zip"
    assert path_v2.name == f"{spec_v2.id}-{spec_v2.version}-{spec_v2.archive_sha256[:16]}.zip"
    assert path_v1 != path_v2

    # Same version tag: caching v1 must not stop v2 from downloading.
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    assert path_v1.is_file()
    env["current"]["spec"] = spec_v2
    patchops.update(backend, PATCH_ID)
    assert path_v2.is_file()
    assert len(env["fetched"]) == 2
    assert env["fetched"][1][1] == spec_v2.archive_sha256


def test_outdated_state_compares_record_without_hashing_game_files(env, monkeypatch):
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    assert patchops.state(backend, PATCH_ID) == "applied"

    env["current"]["spec"] = env["spec_v2"]
    monkeypatch.setattr(
        patches, "_hash_file", lambda *a, **k: pytest.fail("outdated must not hash game files")
    )
    assert patchops.state(backend, PATCH_ID) == "outdated"


def test_update_happy_path_keeps_original_backups_and_remove_restores_originals(env):
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1

    env["current"]["spec"] = env["spec_v2"]
    patchops.update(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patchops.state(backend, PATCH_ID) == "applied"
    assert KIND in env["refreshed"]

    # Backups recorded after update must be the ORIGINAL Japanese files, not UPLOAD_V1.
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v2"].archive_sha256
    assert records[0].files[0].pre_sha256 == _sha(ORIGINAL)

    # Removing after update restores the ORIGINAL bytes byte for byte.
    patchops.remove(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert patchops.state(backend, PATCH_ID) == "pristine"


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: download.DownloadError("connection reset"),
        lambda: download.IntegrityError("sha256 mismatch"),
    ],
    ids=["download_error", "integrity_error"],
)
def test_update_with_failing_download_leaves_old_patch_untouched(env, monkeypatch, exc_factory):
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v2"]

    def fail_download(*a, **k):
        raise exc_factory()

    monkeypatch.setattr(download, "download", fail_download)
    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)

    # Old patch stays applied and completely untouched.
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v1"].archive_sha256
    if isinstance(exc_factory(), download.IntegrityError):
        msg = str(caught.value).lower()
        assert "changed" in msg
        assert "later" in msg
        assert "nadare update" not in msg
        assert "sha256" not in msg


def test_update_when_index_pins_new_build_leaves_working_patch_untouched(env):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    fetched_before = len(env["fetched"])

    # Index advances to a new game build (different before_sha256) while on the old build.
    zip_next = tmp_path / "next_build.zip"
    spec_next = _spec_for(zip_next, UPLOAD_V2, before=NEW_GAME_BUILD)
    env["zips"][spec_next.archive_sha256] = zip_next
    env["current"]["spec"] = spec_next

    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)

    msg = str(caught.value).lower()
    assert "different game build" in msg or "game build" in msg
    assert "update the game" in msg
    assert "stays applied" in msg
    assert "original files were restored" not in msg
    # Working patch stays untouched and nothing was downloaded.
    assert len(env["fetched"]) == fetched_before
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v1"].archive_sha256


def test_update_when_game_updated_on_disk_refuses_without_downloading(env):
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    backend.update()
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD
    fetched_before = len(env["fetched"])

    # Index still pins the old build (before=ORIGINAL).
    env["current"]["spec"] = env["spec_v2"]
    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)

    msg = str(caught.value).lower()
    assert "different game build" in msg or "game build" in msg
    assert "original files were restored" not in msg
    assert len(env["fetched"]) == fetched_before
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD


def test_update_refuses_new_spec_dest_missing_from_record_and_disk(env):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    patchops.apply(backend, PATCH_ID)
    fetched_before = len(env["fetched"])

    extra_after = b"extra file patched\n" * 10
    extra_before = b"extra file original\n" * 10
    zip_extra = tmp_path / "extra.zip"
    sha_extra, size_extra = _make_zip(
        zip_extra, {"table_data.bytes": UPLOAD_V2, "extra.bytes": extra_after}
    )
    spec_extra = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=sha_extra,
        archive_size=size_extra,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(UPLOAD_V2),
                size=len(UPLOAD_V2),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
            patches.PatchFile(
                member="extra.bytes",
                dest="Data/NewSubdir/missing.r",
                sha256=_sha(extra_after),
                size=len(extra_after),
                before_sha256=_sha(extra_before),
                before_size=len(extra_before),
            ),
        ),
    )
    env["zips"][sha_extra] = zip_extra
    env["current"]["spec"] = spec_extra

    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)

    assert "game build" in str(caught.value).lower()
    assert len(env["fetched"]) == fetched_before
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1


def test_update_when_apply_fails_after_revert_keeps_originals_and_says_so(env):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)
    patchops.apply(backend, PATCH_ID)

    # 2-file spec: file 1 succeeds and is written to disk, then file 2 fails its member pin.
    bad_zip = tmp_path / "bad_member.zip"
    bad_archive_sha, bad_archive_size = _make_zip(
        bad_zip, {"table_data.bytes": UPLOAD_V2, "second.bytes": b"wrong member"}
    )
    bad_spec = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=bad_archive_sha,
        archive_size=bad_archive_size,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(UPLOAD_V2),
                size=len(UPLOAD_V2),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
            patches.PatchFile(
                member="second.bytes",
                dest="Data/second.r",
                sha256=_sha(after2),
                size=len(after2),
                before_sha256=_sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )
    env["zips"][bad_archive_sha] = bad_zip
    env["current"]["spec"] = bad_spec

    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)

    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert (root / "Data/second.r").read_bytes() == orig2
    assert patches.applied_patches(root) == ()
    assert "original" in str(caught.value).lower()

    # After the failed apply rolled back, a subsequent good apply succeeds (no leftover backups).
    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2


def test_update_when_already_current_is_a_noop(env):
    backend, events = env["backend"], env["events"]
    patchops.apply(backend, PATCH_ID)
    events.clear()
    env["fetched"].clear()

    patchops.update(backend, PATCH_ID)
    assert env["fetched"] == []
    assert any("already up to date" in ev.message.lower() for ev in events)


def test_update_requires_applied_record(env):
    with pytest.raises(backends.BackendError, match="not applied"):
        patchops.update(env["backend"], PATCH_ID)


def test_remove_works_with_index_disabled_and_no_network(tmp_path, monkeypatch):
    root = tmp_path / "install"
    (root / "Data").mkdir(parents=True)
    (root / "Data/table.r").write_bytes(ORIGINAL)
    real_id = patchspecs.KIBOTL_EN.id
    real_kind = patchspecs.KIBOTL_EN_RULE.kind
    zip_v1 = tmp_path / "v1.zip"
    zip_v2 = tmp_path / "v2.zip"
    spec_v1 = dataclasses.replace(_spec_for(zip_v1, UPLOAD_V1), id=real_id)
    spec_v2 = dataclasses.replace(_spec_for(zip_v2, UPLOAD_V2), id=real_id)
    monkeypatch.setitem(patchspecs.SPECS, real_kind, (spec_v1,))
    # Apply from an index-updated spec (v2), then disable the index completely and remove.
    monkeypatch.setattr(
        patchspecs, "refresh", lambda kind: (spec_v2,) if kind == real_kind else ()
    )
    monkeypatch.setattr(download, "download", lambda url, dest, **k: shutil.copyfile(zip_v2, dest))
    ctx = make_context(tmp_path, real_kind)
    backend = FakeBackend(ctx, root)
    backend.kind = real_kind

    patchops.apply(backend, real_id)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2

    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    read_cached_calls: list[str] = []
    real_read_cached = indexfetch.read_cached

    def spy_read_cached(rel, *args, **kwargs):
        read_cached_calls.append(rel)
        return real_read_cached(rel, *args, **kwargs)

    monkeypatch.setattr(indexfetch, "read_cached", spy_read_cached)
    monkeypatch.setattr(
        indexfetch, "fetch_json", lambda *a, **k: pytest.fail("remove must not touch network")
    )
    monkeypatch.setattr(
        indexfetch, "open_bytes", lambda *a, **k: pytest.fail("remove must not touch network")
    )
    monkeypatch.setattr(
        patchspecs, "refresh", lambda *a, **k: pytest.fail("remove must not call refresh")
    )
    patchops.remove(backend, real_id)
    assert read_cached_calls == [patchspecs.KIBOTL_EN_RULE.index_file]
    assert (root / "Data/table.r").read_bytes() == ORIGINAL


def test_warn_if_undone_works_with_index_specs(env):
    backend, events = env["backend"], env["events"]
    patchops.apply(backend, PATCH_ID)

    # Index advances to v2 while v1 is still intact on disk -> NOT undone!
    env["current"]["spec"] = env["spec_v2"]
    events.clear()
    assert patchops.warn_if_undone(backend) is False

    # Game repair overwrites the patched file -> undone!
    backend.repair()
    assert patchops.warn_if_undone(backend) is True
    assert any("undone" in ev.message.lower() for ev in events)


def test_update_patch_policy_worker_busy_and_cli(env, tmp_path, monkeypatch, capsys):
    # Policy table & busy labels.
    policy = operation_policy.policy_for("update_patch")
    assert policy.locked and policy.mutating and policy.reprobe and policy.wait_for_paused
    assert policy.cancellable
    assert busy.OPERATION_TITLES["update_patch"] == "Updating the patch"
    assert busy._BUSY_STATES["update_patch"] == "updating"

    # CLI _operation_lock holds the install lock for `patch update`.
    with app._operation_lock("patch", "azur", "update"):
        assert process.operation_in_progress("azur")
    assert not process.operation_in_progress("azur")

    # Worker runs update_patch.
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v2"]
    worker = BackendWorker(backend.ctx.config, backend.ctx.name, "update_patch", payload=PATCH_ID)
    assert worker._dispatch(backend) == 0
    assert (backend.root / "Data/table.r").read_bytes() == UPLOAD_V2

    # CLI `patch status` prints outdated and `patch update` updates it.
    patchops.remove(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v1"]
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v2"]

    cfg_path = tmp_path / "config.json"
    config = Config.load(cfg_path)
    config.add_profile("azur", backend="wine", title="Azur")
    config.save()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)

    assert app.run_cli(["-c", str(cfg_path), "patch", "status", "-p", "azur"]) == 0
    assert "outdated" in capsys.readouterr().out
    assert app.run_cli(["-c", str(cfg_path), "patch", "update", "-p", "azur"]) == 0
    assert (backend.root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patchops.state(backend, PATCH_ID) == "applied"
    assert re.search(
        r"\bupdate\b",
        app.build_parser()._subparsers._group_actions[0].choices["patch"].format_help(),
    )


# ---- review fixes (Claude & fix1) --------------------------------------------------------
def test_a_newer_record_is_not_outdated_when_only_the_builtin_pin_is_known(env, monkeypatch):
    """Index unreachable and cache gone: the built-in pin is older than what is applied.

    Offering "Update" would download the older upload (or fail); it must not be offered.
    ``state()`` reports ``applied`` without hashing against the older spec, and ``apply()``
    is a no-op with no download and no PatchConflict."""
    backend, root, events = env["backend"], env["root"], env["events"]
    env["current"]["spec"] = env["spec_v2"]  # applied from an index pin
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v1"]  # back to the built-in fallback (SPECS[KIND])
    assert not patchops.is_outdated(backend, env["spec_v1"])

    orig_status = patches.status
    monkeypatch.setattr(
        patches,
        "status",
        lambda *a, **k: pytest.fail("fallback applied state must not call status"),
    )
    assert patchops.state(backend, PATCH_ID) == "applied"
    monkeypatch.setattr(patches, "status", orig_status)

    fetched = len(env["fetched"])
    patchops.update(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert len(env["fetched"]) == fetched  # nothing downloaded
    assert patches.applied_patches(root)[0].archive_sha256 == env["spec_v2"].archive_sha256

    # apply() in offline-fallback state sees the recorded files are still patched -> no download.
    events.clear()
    patchops.apply(backend, PATCH_ID)
    assert len(env["fetched"]) == fetched
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert any("already applied" in ev.message.lower() for ev in events)

    # If the game overwrites the file on disk while offline fallback is active, the record says
    # applied but the game undid it: "undone" (was "unknown" before #1894).
    backend.update()
    assert patchops.state(backend, PATCH_ID) == "undone"


def test_release_bump_matching_index_still_offers_update(env, monkeypatch):
    """Record A, built-in bumped to B, index doc also pins B -> outdated and updates to B."""
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1

    # Built-in is bumped to spec_v2; index doc yields a fresh PatchSpec with the same hashes.
    builtin_b = env["spec_v2"]
    index_b = dataclasses.replace(builtin_b)
    assert index_b is not builtin_b
    monkeypatch.setitem(patchspecs.SPECS, KIND, (builtin_b,))
    env["current"]["spec"] = index_b

    assert patchops.is_outdated(backend, index_b) is True
    assert patchops.state(backend, PATCH_ID) == "outdated"
    patchops.update(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patchops.state(backend, PATCH_ID) == "applied"


def test_apply_on_an_outdated_patch_updates_it(env):
    """``nadare patch apply`` with an older upload applied moves to the new one with 1 refresh."""
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v2"]
    env["refreshed"].clear()
    patchops.apply(backend, PATCH_ID)
    assert env["refreshed"] == [KIND]
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patches.applied_patches(root)[0].files[0].pre_sha256 == _sha(ORIGINAL)


def test_apply_after_interrupted_remove_recovers_and_applies(env):
    """Interrupted remove leaves journal + record; apply() recovers first and applies cleanly."""
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    record = patches.applied_patches(root)[0]
    patches._write_journal(root, PATCH_ID, record.files, revert=True)

    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patches.applied_patches(root)[0].archive_sha256 == env["spec_v2"].archive_sha256


@pytest.mark.parametrize("mode", ["apply", "update"])
def test_bad_zip_is_caught_unlinks_cache_and_keeps_originals(env, mode):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    if mode == "update":
        patchops.apply(backend, PATCH_ID)

    bad_zip = tmp_path / "not_a_zip.zip"
    raw = b"this is definitely not a zip archive\n" * 10
    bad_zip.write_bytes(raw)
    bad_spec = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=_sha(raw),
        archive_size=len(raw),
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(UPLOAD_V2),
                size=len(UPLOAD_V2),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
        ),
    )
    env["zips"][bad_spec.archive_sha256] = bad_zip
    env["current"]["spec"] = bad_spec

    with pytest.raises(backends.BackendError) as caught:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    assert "original" in str(caught.value).lower()
    assert not patchops._archive_path(bad_spec).exists()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert not (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID).exists()


@pytest.mark.parametrize("mode", ["apply", "update"])
def test_zlib_error_is_caught_unlinks_cache_and_keeps_originals(env, monkeypatch, mode):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)
    if mode == "update":
        patchops.apply(backend, PATCH_ID)

    zip_two = tmp_path / "two_zlib.zip"
    sha_two, size_two = _make_zip(
        zip_two, {"table_data.bytes": UPLOAD_V2, "second.bytes": after2}
    )
    two_spec = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=sha_two,
        archive_size=size_two,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(UPLOAD_V2),
                size=len(UPLOAD_V2),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
            patches.PatchFile(
                member="second.bytes",
                dest="Data/second.r",
                sha256=_sha(after2),
                size=len(after2),
                before_sha256=_sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )
    env["zips"][sha_two] = zip_two
    env["current"]["spec"] = two_spec

    real_extract = patches._extract_member
    seen_members: list[str] = []

    def fail_second_extract(archive, item, target, mode_val=0o644):
        seen_members.append(item.member)
        if item.member == "second.bytes":
            # Confirm file 1 was really written before file 2 fails.
            assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
            raise zlib.error("Error -3 while decompressing data")
        return real_extract(archive, item, target, mode_val)

    monkeypatch.setattr(patches, "_extract_member", fail_second_extract)
    with pytest.raises(backends.BackendError) as caught:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    assert seen_members == ["table_data.bytes", "second.bytes"]
    assert "original" in str(caught.value).lower()
    assert not patchops._archive_path(two_spec).exists()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert (root / "Data/second.r").read_bytes() == orig2
    assert not (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID).exists()
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()


def test_the_busy_title_of_other_operations_is_unchanged():
    """Only the patch operations get the friendly title at start (rc scope)."""
    assert busy.start_label("install") == "Install"
    assert busy.start_label("repair") == "Repair"
    assert busy.start_label("update_patch") == "Updating the patch"
    assert busy.start_label("apply_patch") == "Applying the patch"


# ---- fix2 tests (review round 2) ---------------------------------------------------------


def _make_two_file_spec(
    tmp_path: Path,
    name: str,
    after1: bytes,
    after2: bytes,
    orig2: bytes,
    *,
    member2_bytes: bytes | None = None,
) -> tuple[patches.PatchSpec, Path]:
    zip_path = tmp_path / name
    m2 = member2_bytes if member2_bytes is not None else after2
    archive_sha, archive_size = _make_zip(
        zip_path,
        {"table_data.bytes": after1, "second.bytes": m2},
    )
    spec = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=archive_sha,
        archive_size=archive_size,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(after1),
                size=len(after1),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
            patches.PatchFile(
                member="second.bytes",
                dest="Data/second.r",
                sha256=_sha(after2),
                size=len(after2),
                before_sha256=_sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )
    return spec, zip_path


def test_symlinked_install_dir_apply_state_update_remove(env):
    backend, real_root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    link = tmp_path / "install-symlink"
    link.symlink_to(real_root)
    backend.root = link

    # apply v1 through symlink
    patchops.apply(backend, PATCH_ID)
    assert (real_root / "Data/table.r").read_bytes() == UPLOAD_V1
    assert patchops.state(backend, PATCH_ID) == "applied"

    # update to v2 through symlink
    env["current"]["spec"] = env["spec_v2"]
    assert patchops.state(backend, PATCH_ID) == "outdated"
    patchops.update(backend, PATCH_ID)
    assert (real_root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert patchops.state(backend, PATCH_ID) == "applied"

    # offline fallback state() & apply() through symlink
    env["current"]["spec"] = env["spec_v1"]
    fetched_before = len(env["fetched"])
    assert patchops.state(backend, PATCH_ID) == "applied"
    patchops.apply(backend, PATCH_ID)
    assert len(env["fetched"]) == fetched_before

    # remove through symlink
    patchops.remove(backend, PATCH_ID)
    assert (real_root / "Data/table.r").read_bytes() == ORIGINAL
    assert patchops.state(backend, PATCH_ID) == "pristine"


def test_removed_parent_folder_gives_plain_messages_and_no_raw_patch_error(env):
    backend, root = env["backend"], env["root"]
    # Apply v2, then switch to offline fallback v1 and remove Data/.
    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    shutil.rmtree(root / "Data")
    env["current"]["spec"] = env["spec_v1"]
    fetched_before = len(env["fetched"])

    # state() must not leak raw PatchError; the recorded files are gone, so "undone" (#1894).
    assert patchops.state(backend, PATCH_ID) == "undone"

    # apply() must give the plain different-build message without downloading.
    with pytest.raises(backends.BackendError) as caught_apply:
        patchops.apply(backend, PATCH_ID)
    assert "another build" in str(caught_apply.value).lower()
    assert "cannot resolve" not in str(caught_apply.value).lower()
    assert len(env["fetched"]) == fetched_before

    # remove() works directly while Data/ is still removed (record dropped).
    patchops.remove(backend, PATCH_ID)
    assert patches.applied_patches(root) == ()
    assert not (root / "Data").exists()

    # update() against a newer spec with Data/ removed gives the plain different-build message.
    (root / "Data").mkdir()
    (root / "Data/table.r").write_bytes(ORIGINAL)
    env["current"]["spec"] = env["spec_v1"]
    patchops.apply(backend, PATCH_ID)
    shutil.rmtree(root / "Data")
    env["current"]["spec"] = env["spec_v2"]
    fetched_before = len(env["fetched"])

    with pytest.raises(backends.BackendError) as caught_update:
        patchops.update(backend, PATCH_ID)
    assert "different game build" in str(caught_update.value).lower()
    assert "cannot resolve" not in str(caught_update.value).lower()
    assert len(env["fetched"]) == fetched_before

    # Symlinked target during update() also turns PatchError into plain different-build refusal.
    (root / "Data").mkdir()
    outside = env["tmp_path"] / "outside.r"
    outside.write_bytes(ORIGINAL)
    (root / "Data/table.r").symlink_to(outside)
    with pytest.raises(backends.BackendError) as caught_symlink:
        patchops.update(backend, PATCH_ID)
    assert "different game build" in str(caught_symlink.value).lower()
    assert "symlink" not in str(caught_symlink.value).lower()
    assert len(env["fetched"]) == fetched_before


def test_offline_fallback_apply_after_repair_drops_stale_record_and_applies_v1(env, monkeypatch):
    backend, root = env["backend"], env["root"]
    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2

    # Repair puts ORIGINAL back; state.json still holds v2.
    backend.repair()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    before_stat = (root / "Data/table.r").stat()

    # Spy on revert to prove dropping the stale record leaves the game file untouched.
    revert_checked: list[bool] = []
    real_revert = patches.revert

    def spy_revert(r, pid, *, keep_backups=True):
        real_revert(r, pid, keep_backups=keep_backups)
        after_revert = (root / "Data/table.r").stat()
        assert after_revert.st_ino == before_stat.st_ino
        assert after_revert.st_mtime_ns == before_stat.st_mtime_ns
        assert (root / "Data/table.r").read_bytes() == ORIGINAL
        assert patches.applied_patches(root) == ()
        assert not (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / pid).exists()
        revert_checked.append(True)

    monkeypatch.setattr(patches, "revert", spy_revert)

    # Offline fallback: spec is the SPECS object (v1).
    env["current"]["spec"] = env["spec_v1"]
    assert patchspecs.is_builtin_fallback(env["spec_v1"]) is True

    patchops.apply(backend, PATCH_ID)
    assert revert_checked == [True]
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v1"].archive_sha256


def test_offline_fallback_apply_after_game_update_refuses_without_downloading(env):
    backend, root = env["backend"], env["root"]
    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    backend.update()
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD

    env["current"]["spec"] = env["spec_v1"]
    fetched_before = len(env["fetched"])

    with pytest.raises(backends.BackendError) as caught:
        patchops.apply(backend, PATCH_ID)

    assert "another build" in str(caught.value).lower()
    assert len(env["fetched"]) == fetched_before
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v2"].archive_sha256


def test_same_hash_record_apply_after_game_update_refuses_without_downloading(env):
    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)
    # Remove cached archive so any attempt to download is observable.
    patchops._archive_path(env["spec_v1"]).unlink(missing_ok=True)
    backend.update()
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD
    fetched_before = len(env["fetched"])

    with pytest.raises(backends.BackendError) as caught:
        patchops.apply(backend, PATCH_ID)

    assert "another build" in str(caught.value).lower()
    assert len(env["fetched"]) == fetched_before
    records = patches.applied_patches(root)
    assert len(records) == 1
    assert records[0].archive_sha256 == env["spec_v1"].archive_sha256


def test_record_with_some_files_still_patched_refuses_before_downloading(env):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)

    two_spec, two_zip = _make_two_file_spec(tmp_path, "two.zip", UPLOAD_V1, after2, orig2)
    env["zips"][two_spec.archive_sha256] = two_zip
    env["current"]["spec"] = two_spec
    patchops.apply(backend, PATCH_ID)

    # Remove the cached zip so any download attempt is observable.
    patchops._archive_path(two_spec).unlink(missing_ok=True)
    fetched_before = len(env["fetched"])

    # Overwrite only file 2 so file 1 is still patched (partial record).
    (root / "Data/second.r").write_bytes(orig2)
    with pytest.raises(backends.BackendError) as caught:
        patchops.apply(backend, PATCH_ID)

    msg = str(caught.value).lower()
    assert "undone" in msg
    assert "off" in msg or "remove" in msg
    assert "again" in msg
    assert len(env["fetched"]) == fetched_before
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    assert (root / "Data/second.r").read_bytes() == orig2
    assert len(patches.applied_patches(root)) == 1


@pytest.mark.parametrize("mode", ["apply", "update"])
def test_failed_rollback_reports_repair_files_and_later_recover_restores(env, monkeypatch, mode):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)
    if mode == "update":
        patchops.apply(backend, PATCH_ID)

    bad_spec, bad_zip = _make_two_file_spec(
        tmp_path, "bad_rb.zip", UPLOAD_V2, after2, orig2, member2_bytes=b"corrupted bytes"
    )
    env["zips"][bad_spec.archive_sha256] = bad_zip
    env["current"]["spec"] = bad_spec

    real_undo = patches._undo_entries
    undo_calls = 0

    def fail_undo(*args, **kwargs):
        nonlocal undo_calls
        undo_calls += 1
        # In update mode, revert() calls _undo_entries first; fail on apply()'s rollback.
        if mode == "update" and undo_calls == 1:
            return real_undo(*args, **kwargs)
        raise patches.PatchError("simulated rollback I/O failure")

    monkeypatch.setattr(patches, "_undo_entries", fail_undo)
    with pytest.raises(backends.BackendError) as caught:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    msg = str(caught.value)
    assert "repair files" in msg.lower()
    assert "could not" in msg.lower()
    assert "put back" in msg.lower()
    assert "still in place" not in msg.lower()
    assert (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID).is_dir()

    # Once the cause is gone, a later apply or remove runs recover() first and restores originals.
    monkeypatch.setattr(patches, "_undo_entries", real_undo)
    if mode == "apply":
        with pytest.raises(backends.BackendError, match="not applied"):
            patchops.remove(backend, PATCH_ID)
        assert (root / "Data/table.r").read_bytes() == ORIGINAL
        assert (root / "Data/second.r").read_bytes() == orig2
        assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    else:
        good_spec, good_zip = _make_two_file_spec(
            tmp_path, "good_rb.zip", UPLOAD_V2, after2, orig2
        )
        env["zips"][good_spec.archive_sha256] = good_zip
        env["current"]["spec"] = good_spec
        patchops.apply(backend, PATCH_ID)
        assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
        assert (root / "Data/second.r").read_bytes() == after2
        assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()


@pytest.mark.parametrize("mode", ["apply", "update"])
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: RuntimeError("File 'table_data.bytes' is encrypted, password required"),
        lambda: NotImplementedError("That compression method is not supported"),
    ],
    ids=["encrypted_zip", "unsupported_compression"],
)
def test_zip_runtime_and_not_implemented_errors_unlink_cache_and_keep_originals(
    env, monkeypatch, mode, exc_factory
):
    backend, root = env["backend"], env["root"]
    if mode == "update":
        patchops.apply(backend, PATCH_ID)
        env["current"]["spec"] = env["spec_v2"]
        target_spec = env["spec_v2"]
    else:
        target_spec = env["spec_v1"]

    def fail_extract(*a, **k):
        raise exc_factory()

    monkeypatch.setattr(patches, "_extract_member", fail_extract)
    with pytest.raises(backends.BackendError) as caught:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    msg = str(caught.value).lower()
    assert "damaged zip" in msg
    assert "original" in msg
    assert not patchops._archive_path(target_spec).exists()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL


@pytest.mark.parametrize("mode", ["apply", "update"])
def test_oserror_in_apply_and_update_gives_plain_cause_message(env, monkeypatch, mode):
    import errno

    backend, root = env["backend"], env["root"]
    if mode == "update":
        patchops.apply(backend, PATCH_ID)
        env["current"]["spec"] = env["spec_v2"]

    # OSError during patches.apply with successful rollback names the cause and originals.
    real_extract = patches._extract_member

    def fail_enospc(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(patches, "_extract_member", fail_enospc)
    with pytest.raises(backends.BackendError) as caught:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    msg = str(caught.value)
    assert "No space left on device" in msg
    assert "original" in msg.lower()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL

    # OSError during rollback raises PatchRollbackError -> "Repair files", NOT "still in place".
    if mode == "update":
        # Re-apply v1 first since the previous failed update reverted v1.
        monkeypatch.setattr(patches, "_extract_member", real_extract)
        env["current"]["spec"] = env["spec_v1"]
        patchops.apply(backend, PATCH_ID)
        env["current"]["spec"] = env["spec_v2"]
        monkeypatch.setattr(patches, "_extract_member", fail_enospc)

    real_undo = patches._undo_entries
    rb_undo_calls = 0

    def fail_undo_oserror(*a, **k):
        nonlocal rb_undo_calls
        rb_undo_calls += 1
        # In update mode, revert() calls _undo_entries first; fail on apply()'s rollback.
        if mode == "update" and rb_undo_calls == 1:
            return real_undo(*a, **k)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(patches, "_undo_entries", fail_undo_oserror)
    with pytest.raises(backends.BackendError) as caught_rb:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)

    assert "repair files" in str(caught_rb.value).lower()
    assert "still in place" not in str(caught_rb.value).lower()
    (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).unlink(missing_ok=True)

    # OSError earlier (e.g. recover / permission error) also wraps into BackendError with cause.
    def fail_eacces(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(patches, "recover", fail_eacces)
    with pytest.raises(backends.BackendError) as caught_perm:
        if mode == "apply":
            patchops.apply(backend, PATCH_ID)
        else:
            patchops.update(backend, PATCH_ID)
    assert "Permission denied" in str(caught_perm.value)


@pytest.mark.parametrize("mode", ["apply", "update"])
def test_failed_state_save_rolls_back_so_originals_really_are_in_place(env, monkeypatch, mode):
    # Claude review of fix 2: an OSError saving state.json after every file was written left the
    # patched files on disk while the message said the originals were in place.
    backend, root = env["backend"], env["root"]
    if mode == "update":
        patchops.apply(backend, PATCH_ID)
        env["current"]["spec"] = env["spec_v2"]
    real_save = patches._save_state
    calls = {"n": 0}

    def failing_save(*a, **k):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(patches, "_save_state", failing_save)
    if mode == "update":
        # The revert before the new apply also saves state: fail only the apply's save.
        def fail_second(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_save(*a, **k)
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(patches, "_save_state", fail_second)
    with pytest.raises(backends.BackendError, match="No space left on device") as info:
        getattr(patchops, mode)(backend, PATCH_ID)
    assert "still in place" in str(info.value)
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert not (root / ".nadare-patches/journal.json").exists()
    assert patches.applied_patches(root) == ()
    assert not (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID).exists()
    assert calls["n"] == (2 if mode == "update" else 1)
    monkeypatch.setattr(patches, "_save_state", real_save)
    patchops.apply(backend, PATCH_ID)
    assert patchops.state(backend, PATCH_ID) == "applied"


# ------------------------------------------------------------------ fix3 review round 3 tests


def test_failed_rollback_then_repair_with_corrupt_backup_allows_next_apply(env, monkeypatch):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)

    bad_spec, bad_zip = _make_two_file_spec(
        tmp_path, "bad_f3.zip", UPLOAD_V2, after2, orig2, member2_bytes=b"corrupted bytes"
    )
    env["zips"][bad_spec.archive_sha256] = bad_zip
    env["current"]["spec"] = bad_spec

    real_undo = patches._undo_entries
    monkeypatch.setattr(
        patches, "_undo_entries", lambda *a, **k: (_ for _ in ()).throw(patches.PatchError("io"))
    )
    with pytest.raises(backends.BackendError, match="Repair files"):
        patchops.apply(backend, PATCH_ID)

    journal_path = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    backup_dir = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID
    assert journal_path.exists()

    # Corrupt the backup, then run Repair so pristine files are back on disk.
    (backup_dir / "Data/table.r").write_bytes(b"corrupt backup bytes\n")
    backend.repair()
    (root / "Data/second.r").write_bytes(orig2)

    # Next apply runs recover() first, succeeds even with corrupt backup, and clears the journal.
    monkeypatch.setattr(patches, "_undo_entries", real_undo)
    good_spec, good_zip = _make_two_file_spec(tmp_path, "good_f3.zip", UPLOAD_V2, after2, orig2)
    env["zips"][good_spec.archive_sha256] = good_zip
    env["current"]["spec"] = good_spec
    patchops.apply(backend, PATCH_ID)

    assert not journal_path.exists()
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V2
    assert (root / "Data/second.r").read_bytes() == after2


def test_failed_rollback_then_game_update_recover_keeps_new_build(env, monkeypatch):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original second file\n" * 30
    after2 = b"english second file\n" * 32
    (root / "Data/second.r").write_bytes(orig2)

    bad_spec, bad_zip = _make_two_file_spec(
        tmp_path, "bad_upd_f3.zip", UPLOAD_V2, after2, orig2, member2_bytes=b"corrupted bytes"
    )
    env["zips"][bad_spec.archive_sha256] = bad_zip
    env["current"]["spec"] = bad_spec

    real_undo = patches._undo_entries
    monkeypatch.setattr(
        patches, "_undo_entries", lambda *a, **k: (_ for _ in ()).throw(patches.PatchError("io"))
    )
    with pytest.raises(backends.BackendError, match="Repair files"):
        patchops.apply(backend, PATCH_ID)

    journal_path = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    assert journal_path.exists()

    # Game update installs NEW_GAME_BUILD before recover() runs.
    backend.update()
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD

    monkeypatch.setattr(patches, "_undo_entries", real_undo)
    assert patches.recover(root) == [PATCH_ID]
    assert not journal_path.exists()
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD
    assert (root / "Data/second.r").read_bytes() == orig2


def test_removed_folder_of_recorded_file_allows_remove_and_warn_if_undone(env):
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    orig2 = b"original sub file\n" * 30
    after2 = b"english sub file\n" * 32
    (root / "Sub").mkdir(parents=True)
    (root / "Sub/second.r").write_bytes(orig2)

    zip_path = tmp_path / "two_dirs.zip"
    archive_sha, archive_size = _make_zip(
        zip_path, {"table_data.bytes": UPLOAD_V1, "second.bytes": after2}
    )
    spec = patches.PatchSpec(
        id=PATCH_ID,
        title="Test English",
        version="v1.0-100",
        url="https://github.com/KiboTL/text/releases/download/v1.0-100/en.zip",
        archive_sha256=archive_sha,
        archive_size=archive_size,
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=_sha(UPLOAD_V1),
                size=len(UPLOAD_V1),
                before_sha256=_sha(ORIGINAL),
                before_size=len(ORIGINAL),
            ),
            patches.PatchFile(
                member="second.bytes",
                dest="Sub/second.r",
                sha256=_sha(after2),
                size=len(after2),
                before_sha256=_sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )
    env["zips"][spec.archive_sha256] = zip_path
    env["current"]["spec"] = spec
    patchops.apply(backend, PATCH_ID)

    # Game update removes Sub/.
    shutil.rmtree(root / "Sub")
    env["events"].clear()

    assert patchops.warn_if_undone(backend) is True
    assert any("undone" in ev.message.lower() for ev in env["events"])

    patchops.remove(backend, PATCH_ID)
    assert patches.applied_patches(root) == ()
    assert (root / "Data/table.r").read_bytes() == ORIGINAL
    assert not (root / "Sub").exists()


def test_remove_oserror_gives_plain_backend_error_naming_cause(env, monkeypatch):
    import errno

    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)

    def fail_enospc(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(patches, "_copy_with_reflink", fail_enospc)
    with pytest.raises(backends.BackendError) as caught:
        patchops.remove(backend, PATCH_ID)
    assert "No space left on device" in str(caught.value)


def test_state_returns_unknown_on_patch_error_or_oserror(env, monkeypatch):
    import errno

    backend, root = env["backend"], env["root"]
    patchops.apply(backend, PATCH_ID)

    # Malformed state.json -> applied_patches raises PatchError -> state returns "unknown".
    state_path = root / patches.STATE_DIR_NAME / patches.STATE_NAME
    saved_state = state_path.read_text(encoding="utf-8")
    state_path.write_text("{not json", encoding="utf-8")
    assert patchops.state(backend, PATCH_ID) == "unknown"
    state_path.write_text(saved_state, encoding="utf-8")

    # OSError from applied_patches -> "unknown".
    real_applied = patches.applied_patches
    monkeypatch.setattr(
        patches,
        "applied_patches",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "Permission denied")),
    )
    assert patchops.state(backend, PATCH_ID) == "unknown"
    monkeypatch.setattr(patches, "applied_patches", real_applied)

    # OSError from _still_patched check (offline fallback v1 while v2 recorded) -> "unknown".
    env["current"]["spec"] = env["spec_v2"]
    patchops.update(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v1"]
    real_hash = patches._hash_file
    monkeypatch.setattr(
        patches,
        "_hash_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "Input/output error")),
    )
    assert patchops.state(backend, PATCH_ID) == "unknown"

    # OSError from patches.status() when no record exists -> "unknown".
    monkeypatch.setattr(patches, "_hash_file", real_hash)
    patchops.remove(backend, PATCH_ID)
    monkeypatch.setattr(
        patches,
        "_hash_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "Input/output error")),
    )
    assert patchops.state(backend, PATCH_ID) == "unknown"


def test_apply_with_stale_record_and_applied_files_drops_record_and_reports_outside_nadare(env):
    backend, root = env["backend"], env["root"]
    # Record v2, repair undoes it, then v1 is placed manually outside Nadare.
    env["current"]["spec"] = env["spec_v2"]
    patchops.apply(backend, PATCH_ID)
    backend.repair()
    (root / "Data/table.r").write_bytes(UPLOAD_V1)
    before_stat = (root / "Data/table.r").stat()

    # Offline fallback resolves spec_v1 (built-in fallback object).
    env["current"]["spec"] = env["spec_v1"]
    assert patchspecs.is_builtin_fallback(env["spec_v1"]) is True
    fetched_before = len(env["fetched"])
    env["events"].clear()

    patchops.apply(backend, PATCH_ID)

    assert len(env["fetched"]) == fetched_before
    assert patches.applied_patches(root) == ()
    assert not (root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / PATCH_ID).exists()
    after_stat = (root / "Data/table.r").stat()
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert (root / "Data/table.r").read_bytes() == UPLOAD_V1
    messages = " ".join(ev.message for ev in env["events"])
    assert "outside Nadare" in messages
    assert "Repair files" in messages


def test_cli_patch_status_asks_the_index_not_only_the_cache(env, tmp_path, monkeypatch, capsys):
    # The cache (specs_for) still holds v1, the index (refresh) already pins v2: `patch status`
    # must say "outdated" with the index's version, as the GUI does, not "applied".
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    monkeypatch.setattr(
        patchspecs, "specs_for", lambda kind: (env["spec_v1"],) if kind == KIND else ()
    )
    newer = dataclasses.replace(env["spec_v2"], version="v1.0-200")
    env["current"]["spec"] = newer
    env["refreshed"].clear()

    cfg_path = tmp_path / "config.json"
    config = Config.load(cfg_path)
    config.add_profile("azur", backend="wine", title="Azur")
    config.save()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)

    assert app.run_cli(["-c", str(cfg_path), "patch", "status", "-p", "azur"]) == 0
    out = capsys.readouterr().out
    assert env["refreshed"] == [KIND]
    assert "outdated" in out
    assert "v1.0-200" in out


def test_cli_patch_status_works_offline(env, tmp_path, monkeypatch, capsys):
    # refresh() never raises on index trouble and falls back to the cache; status still answers.
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    monkeypatch.setattr(patchspecs, "refresh", lambda kind: (env["spec_v1"],))

    cfg_path = tmp_path / "config.json"
    config = Config.load(cfg_path)
    config.add_profile("azur", backend="wine", title="Azur")
    config.save()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)

    assert app.run_cli(["-c", str(cfg_path), "patch", "status", "-p", "azur"]) == 0
    assert "applied" in capsys.readouterr().out


def test_update_stops_when_the_game_changes_between_the_check_and_the_revert(env, monkeypatch):
    # rc4 mutation P09: the build check passes, then the game rewrites its file while the old
    # patch is reverted. Update must stop with the plain race message and never apply over it.
    backend = env["backend"]
    patchops.apply(backend, PATCH_ID)
    env["current"]["spec"] = env["spec_v2"]
    real_revert = patches.revert
    applied: list[object] = []

    def revert_then_game_writes(root, patch_id, **kwargs):
        real_revert(root, patch_id, **kwargs)
        (Path(root) / "Data/table.r").write_bytes(NEW_GAME_BUILD)

    monkeypatch.setattr(patches, "revert", revert_then_game_writes)
    monkeypatch.setattr(patches, "apply", lambda *a, **k: applied.append(a))

    with pytest.raises(backends.BackendError, match="game files changed while updating"):
        patchops.update(backend, PATCH_ID)
    assert applied == []
    assert (backend.root / "Data/table.r").read_bytes() == NEW_GAME_BUILD


def test_update_after_a_game_update_renamed_a_patched_file_and_the_new_pin_drops_one(env):
    """11-27 shape: the update renamed a patched container, another stayed patched, and the
    newer pin covers only the renamed one. Update puts the kept file's original back and
    patches the renamed file; nothing is written at the old, vanished name."""
    backend, root, tmp_path = env["backend"], env["root"], env["tmp_path"]
    renamed_old_jp, kept_jp, renamed_new_jp = b"old-jp\n" * 9, b"kept-jp\n" * 9, b"new-jp\n" * 9
    old_en, kept_en, new_en = b"old-en\n" * 9, b"kept-en\n" * 9, b"new-en\n" * 9
    (root / "Data/old_name.r").write_bytes(renamed_old_jp)
    (root / "Data/kept.r").write_bytes(kept_jp)

    def pf(member, dest, after, before):
        return patches.PatchFile(
            member=member, dest=dest, sha256=_sha(after), size=len(after),
            before_sha256=_sha(before), before_size=len(before),
        )  # fmt: skip

    def spec(name, members, files):
        sha, size = _make_zip(tmp_path / name, members)
        env["zips"][sha] = tmp_path / name
        return dataclasses.replace(
            env["spec_v1"], archive_sha256=sha, archive_size=size, files=files
        )

    old = spec(
        "old.zip",
        {"vstring.bytes": old_en, "startup.r": kept_en},
        (
            pf("vstring.bytes", "Data/old_name.r", old_en, renamed_old_jp),
            pf("startup.r", "Data/kept.r", kept_en, kept_jp),
        ),
    )
    env["current"]["spec"] = old
    patchops.apply(backend, PATCH_ID)

    # The game update: the container moves to a new name with new Japanese contents.
    (root / "Data/old_name.r").unlink()
    (root / "Data/new_name.r").write_bytes(renamed_new_jp)

    env["current"]["spec"] = spec(
        "new.zip",
        {"vstring.bytes": new_en},
        (pf("vstring.bytes", "Data/new_name.r", new_en, renamed_new_jp),),
    )
    patchops.update(backend, PATCH_ID)

    assert (root / "Data/new_name.r").read_bytes() == new_en
    assert (root / "Data/kept.r").read_bytes() == kept_jp
    assert not (root / "Data/old_name.r").exists()
    assert patchops.state(backend, PATCH_ID) == "applied"

    patchops.remove(backend, PATCH_ID)
    assert (root / "Data/new_name.r").read_bytes() == renamed_new_jp
    assert (root / "Data/kept.r").read_bytes() == kept_jp
