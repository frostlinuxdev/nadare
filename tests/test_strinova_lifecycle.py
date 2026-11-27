# SPDX-License-Identifier: GPL-3.0-or-later
"""Strinova install lifecycle: install, update, Verify, Repair, retired files and unsafe trees."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from _backend_helpers import make_context
from conftest import _isolated_environment  # noqa: F401

from nadare.backends.direct_strinova import StrinovaBackend
from nadare.core import directdl as dd
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled
from nadare.core.sources import strinova


def pack(tmp, name, files):
    folder = tmp / name
    folder.mkdir()
    for path, data in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    target = tmp / (name + ".7z")
    subprocess.run(
        ["7z", "a", "-t7z", str(target), "."], cwd=folder, check=True, capture_output=True
    )
    return target.read_bytes()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    old = {"Strinova.exe": b"MZ-old", "keep.bin": b"keep", "removed.bin": b"retired"}
    new = {"Strinova.exe": b"MZ-new", "keep.bin": b"keep"}
    third = {"Strinova.exe": b"MZ-end"}
    patch = {"Strinova.exe": b"MZ-new"}
    packages = {
        "/Game_Release/3/full_zip/full.7z": pack(tmp_path, "third", third),
        "/Game_Release/3/patch_zip/2.7z": pack(tmp_path, "patch3", third),
        "/Game_Release/1/full_zip/full.7z": pack(tmp_path, "old", old),
        "/Game_Release/2/full_zip/full.7z": pack(tmp_path, "new", new),
        "/Game_Release/2/patch_zip/1.7z": pack(tmp_path, "patch", patch),
    }

    def row(hop, path):
        blob = packages[path]
        return f"{hop}|{path}|{hashlib.md5(blob).hexdigest()}|{len(blob)}\n"

    rows = row("1->1", "/Game_Release/1/full_zip/full.7z")
    rows += row("2->2", "/Game_Release/2/full_zip/full.7z")
    rows += row("1->2", "/Game_Release/2/patch_zip/1.7z")
    rows += row("3->3", "/Game_Release/3/full_zip/full.7z")
    rows += row("2->3", "/Game_Release/3/patch_zip/2.7z")
    state = {"target": "1"}

    def fetch(url, headers=None):
        if url == strinova.MANIFEST_URL:
            return f"--last_version--\n{state['target']}\n--download_version--\n{rows}".encode()
        files = {"1": old, "2": new, "3": third}[state["target"]]
        return "".join(
            f"{p}|{hashlib.md5(b).hexdigest()}|{len(b)}\n" for p, b in files.items()
        ).encode()

    transfers = []

    def download(url, destination, **kwargs):
        transfers.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(packages[url.removeprefix(strinova.BASE)])

    monkeypatch.setattr(dd, "download", download)
    backend = StrinovaBackend(
        make_context(tmp_path, "direct-strinova", {"prefix": str(tmp_path / "prefix")})
    )
    backend.install_dir().mkdir(parents=True)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda *a: None)
    monkeypatch.setattr(backend, "plan", lambda: strinova.resolve(fetch, **backend.plan_kwargs()))
    backend._sync_direct()
    assert backend._missing_entries() == []
    backend._fixture_state = state
    backend._fixture_transfers = transfers
    (backend.install_dir() / "user.txt").write_bytes(b"user data")
    return backend


@pytest.fixture
def updated(installed):
    backend = installed
    backend._fixture_state["target"] = "2"
    transfers = backend._fixture_transfers
    transfers.clear()
    backend._sync_direct()
    assert len(transfers) == 1 and "/patch_zip/" in transfers[0]
    assert backend._read_version_file()["version"] == "2"
    assert (backend.install_dir() / "Strinova.exe").read_bytes() == b"MZ-new"
    return backend


def test_successful_incremental_is_verified(updated):
    assert updated._missing_entries() == [], "successful incremental requires full archive again"


def test_incremental_removes_previously_managed_retired_file(updated):
    root = updated.install_dir()
    assert (root / "keep.bin").read_bytes() == b"keep"
    assert not (root / "removed.bin").exists(), "retired managed file survives update"


@pytest.mark.parametrize("damage", ["same-size", "missing"])
def test_repair_checks_target_manifest(updated, damage):
    root = updated.install_dir()
    transfers = updated._fixture_transfers
    transfers.clear()
    updated.repair()
    assert transfers == []
    if damage == "same-size":
        (root / "keep.bin").write_bytes(b"oops")
    else:
        (root / "keep.bin").unlink()
    assert updated._missing_entries()
    updated.repair()
    assert len(transfers) == 1 and "/full_zip/" in transfers[0]
    assert (root / "keep.bin").read_bytes() == b"keep"
    assert updated._missing_entries() == []


def test_two_incrementals_preserve_complete_inventory(updated):
    updated._fixture_state["target"] = "3"
    updated._fixture_transfers.clear()
    updated.repair()
    assert len(updated._fixture_transfers) == 1
    assert "/patch_zip/2.7z" in updated._fixture_transfers[0]
    assert updated._missing_entries() == []
    assert not (updated.install_dir() / "keep.bin").exists()
    assert (updated.install_dir() / "user.txt").read_bytes() == b"user data"


@pytest.mark.parametrize("unsafe", ["traversal", "absolute", "linked/victim.bin", "link.bin"])
def test_retired_inventory_cannot_escape_or_follow_links(updated, tmp_path, unsafe):
    root = updated.install_dir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.bin"
    victim.write_bytes(b"keep")
    if unsafe == "traversal":
        unsafe = os.path.relpath(victim, root)
    elif unsafe == "absolute":
        unsafe = str(victim)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "link.bin").symlink_to(victim)
    # Forge a prior receipt just as an earlier full archive would own these paths.
    record = root / dd.STATE_DIR / "unsafe.done"
    record.write_text(
        json.dumps(
            {
                "game": "strinova",
                "url": strinova.BASE + "/old.7z",
                "members": [unsafe],
                "member_sizes": {unsafe: 4},
                "member_crc32": {unsafe: dd._crc32_file(victim)},
            }
        )
    )
    updated._fixture_state["target"] = "3"
    updated._sync_direct()
    assert victim.read_bytes() == b"keep"
    assert (root / "link.bin").is_symlink()
    assert (root / "linked").is_symlink()
    assert not (root / "keep.bin").exists()
    assert (root / "user.txt").read_bytes() == b"user data"


def test_repair_pending_incremental_checks_unchanged_files(installed):
    installed._fixture_state["target"] = "2"
    installed._fixture_transfers.clear()
    (installed.install_dir() / "keep.bin").write_bytes(b"oops")
    installed.repair()
    assert ["/patch_zip/" in url for url in installed._fixture_transfers] == [True, False]
    assert installed._missing_entries() == []
    assert (installed.install_dir() / "keep.bin").read_bytes() == b"keep"
    assert not (installed.install_dir() / "removed.bin").exists()


def test_full_fallback_reconciles_inventory(installed):
    installed._fixture_state["target"] = "3"  # No 1 -> 3 patch.
    installed._fixture_transfers.clear()
    installed.repair()
    assert len(installed._fixture_transfers) == 1
    assert "/full_zip/" in installed._fixture_transfers[0]
    assert installed._missing_entries() == []
    assert not (installed.install_dir() / "removed.bin").exists()
    assert not (installed.install_dir() / "keep.bin").exists()
    assert (installed.install_dir() / "user.txt").read_bytes() == b"user data"


def _replace_with_hazard(root, name, hazard, victim):
    """Put a symlink (to ``victim``) or a folder where ``name`` should be a file."""
    (root / name).unlink()
    if hazard == "symlink":
        (root / name).symlink_to(victim)
    else:
        (root / name).mkdir()


@pytest.mark.parametrize("hazard", ["symlink", "directory"])
def test_verify_reports_target_hazard(updated, tmp_path, hazard):
    root = updated.install_dir()
    (updated.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"keep")
    _replace_with_hazard(root, "keep.bin", hazard, victim)
    result = updated.verify()
    assert result["verified"] is False
    assert result["checks"].get("files") is False
    assert "keep.bin" in result["message"]
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("hazard", ["symlink", "directory"])
@pytest.mark.parametrize("action", ["install", "update", "repair"])
def test_actions_refuse_target_hazard(updated, tmp_path, hazard, action):
    root = updated.install_dir()
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"keep")
    _replace_with_hazard(root, "keep.bin", hazard, victim)
    with pytest.raises(BackendError, match="keep.bin"):
        getattr(updated, action)()
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("action", ["install", "update", "repair"])
def test_symlinked_state_dir_refuses_before_touching_receipts(
    updated, tmp_path, action, monkeypatch
):
    root = updated.install_dir()
    state = root / dd.STATE_DIR
    outside = tmp_path / "outside-state"
    outside.mkdir()
    receipt = outside / "strinova-inventory.done"
    receipt.write_text((state / "strinova-inventory.done").read_text())
    sidecar = outside / "strinova-inventory.members"
    sidecar.write_text("Strinova.exe\nkeep.bin\n")
    archive = outside / "Game_Release" / "1" / "full_zip"
    archive.mkdir(parents=True)
    exe = root / "Strinova.exe"
    keep = root / "keep.bin"
    record = {
        "game": "strinova",
        "url": strinova.BASE + "/Game_Release/1/full_zip/full.7z",
        "members": ["Strinova.exe", "keep.bin"],
        "member_sizes": {exe.name: exe.stat().st_size, keep.name: keep.stat().st_size},
        "member_crc32": {exe.name: dd._crc32_file(exe), keep.name: dd._crc32_file(keep)},
    }
    (archive / "full.7z.done").write_text(json.dumps(record))
    (tmp_path / "real-state").mkdir()
    state.rename(tmp_path / "real-state" / "moved")
    state.symlink_to(outside, target_is_directory=True)
    real_recorded = dd._recorded_members

    def guarded_recorded(plan, install_root):
        assert not (Path(install_root) / dd.STATE_DIR).is_symlink(), "state read through a link"
        return real_recorded(plan, install_root)

    monkeypatch.setattr(dd, "_recorded_members", guarded_recorded)
    with pytest.raises(BackendError, match="symlink"):
        getattr(updated, action)()
    assert receipt.exists()
    assert sidecar.exists()
    assert (archive / "full.7z.done").exists()
    assert keep.read_bytes() == b"keep"


def test_state_dir_link_planted_mid_sync_is_refused(installed, tmp_path, monkeypatch):
    root = installed.install_dir()
    outside = tmp_path / "outside-state"
    archive = outside / "Game_Release" / "1" / "full_zip"
    archive.mkdir(parents=True)
    keep = root / "keep.bin"
    stale = archive / "full.7z.done"
    stale.write_text(
        json.dumps(
            {
                "game": "strinova",
                "url": strinova.BASE + "/Game_Release/1/full_zip/full.7z",
                "members": ["keep.bin"],
                "member_sizes": {"keep.bin": keep.stat().st_size},
                "member_crc32": {"keep.bin": dd._crc32_file(keep)},
            }
        )
    )
    real_sync = dd.sync

    def plant_after_sync(plan, *args, **kwargs):
        result = real_sync(plan, *args, **kwargs)
        state = root / dd.STATE_DIR
        state.rename(tmp_path / "moved-state")
        state.symlink_to(outside, target_is_directory=True)
        return result

    written = []
    real_write = dd._write_done

    def counting_write(plan, archive, *args, **kwargs):
        if archive.name == "strinova-inventory":
            written.append(archive.name)
        return real_write(plan, archive, *args, **kwargs)

    monkeypatch.setattr(dd, "sync", plant_after_sync)
    monkeypatch.setattr(dd, "_write_done", counting_write)
    installed._fixture_state["target"] = "2"
    with pytest.raises(BackendError, match="symlink"):
        installed.update()
    assert written == [], "a receipt was written through the link"
    assert stale.exists()
    assert keep.read_bytes() == b"keep"


def test_check_update_never_scans_installed_files(updated, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("an update check read the installed files")

    monkeypatch.setattr(dd, "plan_missing", forbidden)
    monkeypatch.setattr(updated, "_target_missing", forbidden)
    updated._latest_path().unlink(missing_ok=True)
    updated.check_update()
    assert updated._read_latest()["version"] == "2"


def test_cancelled_healthy_repair_stays_installed(updated, monkeypatch):
    real_sync = dd.sync

    def cancel_after_sync(plan, *args, **kwargs):
        result = real_sync(plan, *args, **kwargs)
        updated.ctx.cancel.cancel()
        return result

    monkeypatch.setattr(dd, "sync", cancel_after_sync)
    with pytest.raises(Cancelled):
        updated.repair()
    assert not updated._install_marker_path().exists()
    assert updated._read_version_file()["version"] == "2"
    assert updated.status().state == "installed"


def test_healthy_repair_scans_once_and_keeps_receipt(updated, monkeypatch):
    calls = []
    real_missing = updated._target_missing

    def counted(*args, **kwargs):
        calls.append(kwargs.get("verify", True))
        return real_missing(*args, **kwargs)

    def no_rewrite(*args, **kwargs):
        raise AssertionError("a healthy repair rewrote the inventory receipt")

    monkeypatch.setattr(updated, "_target_missing", counted)
    monkeypatch.setattr(dd, "_write_done", no_rewrite)
    updated.repair()
    assert calls == [True], calls


def test_new_inventory_receipt_is_written_before_pruning(installed, monkeypatch):
    receipt = installed.install_dir() / dd.STATE_DIR / "strinova-inventory.done"
    seen = []
    real_prune = dd._prune_dropped_members

    def checking_prune(plan, *args, **kwargs):
        if plan.archives:  # the sync's own prune of the archive plan, not the inventory
            return real_prune(plan, *args, **kwargs)
        record = json.loads(receipt.read_text()) if receipt.exists() else {}
        seen.append(record.get("members"))
        return real_prune(plan, *args, **kwargs)

    monkeypatch.setattr(dd, "_prune_dropped_members", checking_prune)
    installed._fixture_state["target"] = "2"
    installed.update()
    assert seen, "the update never pruned the old inventory"
    assert "keep.bin" in seen[0], seen
    assert "removed.bin" not in seen[0], seen
    assert not (installed.install_dir() / "removed.bin").exists()


def test_missing_entries_reports_the_damaged_file(updated):
    root = updated.install_dir()
    (updated.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    (root / "keep.bin").write_bytes(b"oops")
    assert [entry.path for entry in updated._missing_entries()] == ["keep.bin"]
    result = updated.verify()
    assert result["verified"] is False
    assert result["checks"]["files"] is False
    assert "keep.bin" in result["message"]


def test_full_fallback_uses_profile_download_workers(installed, monkeypatch):
    installed.ctx.profile["download_workers"] = 3
    calls = []
    real_sync = dd.sync

    def counted(plan, *args, **kwargs):
        calls.append(([archive.name for archive in plan.archives], kwargs.get("workers")))
        return real_sync(plan, *args, **kwargs)

    monkeypatch.setattr(dd, "sync", counted)
    installed._fixture_state["target"] = "2"
    (installed.install_dir() / "keep.bin").write_bytes(b"oops")
    installed.repair()
    fallback = [workers for archives, workers in calls if any("full_zip" in a for a in archives)]
    assert fallback, calls
    assert fallback[-1] == 3, calls


def test_fresh_install_prunes_archive_receipts(installed):
    state = installed.install_dir() / dd.STATE_DIR
    names = sorted(path.relative_to(state).as_posix() for path in state.rglob("*.done"))
    assert names == ["strinova-inventory.done"]


def test_retired_file_owned_by_another_slot_is_kept(updated):
    root = updated.install_dir()
    keep = root / "keep.bin"
    (root / dd.STATE_DIR / "other.done").write_text(
        json.dumps(
            {
                "game": "strinova",
                "slot": "dlc",
                "url": "https://resource-download.strinova.com/Client/Win/GameDepot/other.7z",
                "members": ["keep.bin"],
                "member_sizes": {"keep.bin": keep.stat().st_size},
                "member_crc32": {"keep.bin": dd._crc32_file(keep)},
            }
        )
    )
    updated._fixture_state["target"] = "3"
    updated.repair()
    assert keep.read_bytes() == b"keep"


def test_retired_file_changed_by_the_user_is_kept(updated):
    keep = updated.install_dir() / "keep.bin"
    keep.write_bytes(b"my own data")
    updated._fixture_state["target"] = "3"
    updated.repair()
    assert keep.read_bytes() == b"my own data"


def test_failed_target_check_keeps_old_version_and_resume_marker(installed, monkeypatch):
    from dataclasses import replace

    from nadare.core.backends import BackendError

    installed._fixture_state["target"] = "2"
    resolve_plan = installed.plan

    def broken_manifest():
        plan = resolve_plan()
        rows = tuple(dict(row, md5="0" * 32) for row in plan.extra["verification_files"])
        return replace(plan, extra={**plan.extra, "verification_files": rows})

    monkeypatch.setattr(installed, "plan", broken_manifest)
    with pytest.raises(BackendError, match="still damaged"):
        installed._sync_direct()
    assert installed._read_version_file()["version"] == "1"
    assert installed._install_marker_path().exists()
    assert (installed.install_dir() / "removed.bin").read_bytes() == b"retired"


def test_plan_without_verification_rows_is_refused_before_any_download(installed, monkeypatch):
    """The source always sends the row list; a plan without it must stop with a clear error."""
    from dataclasses import replace

    from nadare.core import directdl
    from nadare.core.backends import BackendError

    installed._fixture_state["target"] = "2"
    resolve_plan = installed.plan
    monkeypatch.setattr(installed, "plan", lambda: replace(resolve_plan(), extra={}))
    synced = []
    monkeypatch.setattr(directdl, "sync", lambda *a, **kw: synced.append(a))
    with pytest.raises(BackendError, match="verification"):
        installed.update()
    assert synced == []
    assert installed._read_version_file()["version"] == "1"


def test_an_update_the_launcher_already_applied_refreshes_the_inventory(installed):
    """The publisher launcher patched the tree; Nadare's Update downloads nothing but must
    still move its inventory receipt to the new version (2026-11-27)."""
    root = installed.install_dir()
    (root / "Strinova.exe").write_bytes(b"MZ-new")
    receipt = root / dd.STATE_DIR / "strinova-inventory.done"
    assert json.loads(receipt.read_text())["version"] == "1"
    installed._fixture_state["target"] = "2"
    transfers = installed._fixture_transfers
    transfers.clear()
    installed._sync_direct()
    assert transfers == []
    assert installed._read_version_file()["version"] == "2"
    record = json.loads(receipt.read_text())
    assert record["version"] == "2"
    assert sorted(record["members"]) == ["Strinova.exe", "keep.bin"]
    assert not (root / "removed.bin").exists()  # the retired file is pruned, not left
    assert (root / "user.txt").read_bytes() == b"user data"


def test_a_verified_sync_resets_the_launchers_stuck_patch(installed):
    """Nadare's update checked every file; the launcher's interrupted patch must not stay
    stuck in its config (Neon, 2026-11-27)."""
    from nadare.core import strinova_launcher as sl

    root = installed.install_dir()
    prefix = installed.prefix()
    sl._link_game(sl.game_link(prefix), root, prefix)
    path = sl.config_path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[General]\nCurrentGameVersion=1\nTargetGameVersion=2\nCurrentStage=PATCHSTAGE\n",
        encoding="utf-8",
    )
    installed._fixture_state["target"] = "2"
    installed._sync_direct()
    text = path.read_text(encoding="utf-8")
    assert "CurrentGameVersion=2" in text
    assert "CurrentStage=INITSTAGE" in text


def test_a_sync_creates_no_launcher_config(installed):
    from nadare.core import strinova_launcher as sl

    installed._fixture_state["target"] = "2"
    installed._sync_direct()
    assert not sl.config_path(installed.prefix()).exists()
