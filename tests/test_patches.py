# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch specs, apply/revert, and the guarantees that make a revert trustworthy."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from nadare.core import installlock, patches, patchspecs

BEFORE = b"pristine container bytes\n" * 32
AFTER = b"english container bytes\n" * 40


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_install(tmp_path: Path, rel: str = "Data/table.r", payload: bytes = BEFORE) -> Path:
    root = tmp_path / "install"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return root


def make_archive(tmp_path: Path, members: dict[str, bytes], name: str = "en.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return path


def make_spec(
    archive: Path,
    *,
    rel: str = "Data/table.r",
    member: str = "table_data.bytes",
    after: bytes = AFTER,
    pin_before: bytes | None = BEFORE,
    archive_sha: str | None = None,
) -> patches.PatchSpec:
    return patches.PatchSpec(
        id="test-overlay",
        title="Test overlay",
        version="v1",
        url="https://example.invalid/en.zip",
        archive_sha256=archive_sha or sha(archive.read_bytes()),
        files=(
            patches.PatchFile(
                member=member,
                dest=rel,
                sha256=sha(after),
                size=len(after),
                before_sha256=sha(pin_before) if pin_before is not None else None,
                before_size=len(pin_before) if pin_before is not None else None,
            ),
        ),
    )


# --------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "dest",
    ["../escape.r", "/etc/passwd", "Data/../../x.r", "Data//table.r", "./Data/table.r", ""],
)
def test_a_destination_that_is_not_a_plain_relative_path_is_refused(dest):
    with pytest.raises(ValueError):
        patches.PatchFile(member="m.bytes", dest=dest, sha256=sha(AFTER), size=len(AFTER))


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "0" * 63, "0" * 64 + "\n"])
def test_a_sha256_pin_must_be_lowercase_hex(bad):
    with pytest.raises(ValueError):
        patches.PatchFile(member="m.bytes", dest="Data/x.r", sha256=bad, size=1)
    with pytest.raises(ValueError):
        patches.PatchFile(
            member="m.bytes", dest="Data/x.r", sha256=sha(AFTER), size=1, before_sha256=bad
        )


def test_a_backslash_path_cannot_smuggle_a_windows_separator():
    with pytest.raises(ValueError):
        patches.PatchFile(member="m.bytes", dest="Data\\x.r", sha256=sha(AFTER), size=len(AFTER))


def test_a_spec_cannot_declare_the_same_destination_twice(tmp_path):
    archive = make_archive(tmp_path, {"m.bytes": AFTER})
    item = patches.PatchFile(member="m.bytes", dest="Data/x.r", sha256=sha(AFTER), size=len(AFTER))
    with pytest.raises(ValueError):
        patches.PatchSpec(
            id="dup",
            title="t",
            version="v",
            url="https://example.invalid",
            archive_sha256=sha(archive.read_bytes()),
            files=(item, item),
        )


def test_the_shipped_kibotl_spec_is_well_formed():
    spec = patchspecs.KIBOTL_EN
    assert spec.id == "kibotl-en"
    assert len(spec.files) == 3
    assert {item.member for item in spec.files} == {
        "vstring.bytes",
        "table_data.bytes",
        "static-text.r",
    }
    for item in spec.files:
        assert item.dest.startswith("AzurPromiliaJP_Data/")
        assert not item.dest.startswith("/")
        # A shipped spec is build-pinned: both sides of the change must be declared.
        assert item.before_sha256 and item.before_size is not None
    assert patchspecs.specs_for("azurpromilia-jp-yostar") == (spec,)
    assert patchspecs.specs_for("some-other-game") == ()


# --------------------------------------------------------------------- apply


def test_apply_replaces_the_container_and_records_what_it_did(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    record = patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == AFTER
    assert record.id == "test-overlay"
    assert record.files[0].pre_sha256 == sha(BEFORE)
    backup = root / patches.STATE_DIR_NAME / str(record.files[0].backup)
    assert backup.read_bytes() == BEFORE
    state = json.loads((root / patches.STATE_DIR_NAME / patches.STATE_NAME).read_text())
    assert state["version"] == patches.STATE_VERSION
    assert state["patches"]["test-overlay"]["files"][0]["post_sha256"] == sha(AFTER)
    assert patches.status(root, spec).state == "applied"
    assert [r.id for r in patches.applied_patches(root)] == ["test-overlay"]


def test_applying_the_same_patch_twice_is_a_no_op(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    first = patches.apply(root, spec, archive)
    second = patches.apply(root, spec, archive)

    assert first.applied_at == second.applied_at
    assert (root / "Data/table.r").read_bytes() == AFTER


def test_apply_keeps_the_file_mode(tmp_path):
    root = make_install(tmp_path)
    (root / "Data/table.r").chmod(0o640)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})

    patches.apply(root, make_spec(archive), archive)

    assert (root / "Data/table.r").stat().st_mode & 0o777 == 0o640


def test_a_build_that_does_not_match_the_pin_is_refused_and_nothing_changes(tmp_path):
    root = make_install(tmp_path, payload=b"a different build\n")
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})

    with pytest.raises(patches.PatchConflict) as excinfo:
        patches.apply(root, make_spec(archive), archive)

    assert "not the build" in str(excinfo.value)
    assert (root / "Data/table.r").read_bytes() == b"a different build\n"
    assert not (root / patches.STATE_DIR_NAME / patches.STATE_NAME).exists()
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()


def test_force_overrides_the_before_pin_but_still_backs_up(tmp_path):
    root = make_install(tmp_path, payload=b"some other overlay\n")
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})

    record = patches.apply(root, make_spec(archive), archive, force=True)

    assert (root / "Data/table.r").read_bytes() == AFTER
    backup = root / patches.STATE_DIR_NAME / str(record.files[0].backup)
    assert backup.read_bytes() == b"some other overlay\n"


def test_an_archive_that_fails_its_own_hash_is_never_opened(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    wrong_archive_pin = patches.PatchSpec(
        id=spec.id,
        title=spec.title,
        version=spec.version,
        url=spec.url,
        archive_sha256="0" * 64,
        files=spec.files,
    )
    with pytest.raises(patches.PatchVerificationError):
        patches.apply(root, wrong_archive_pin, archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE


def test_an_archive_with_a_missing_member_leaves_the_install_alone(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"something-else.bytes": AFTER})

    with pytest.raises(patches.PatchError):
        patches.apply(root, make_spec(archive), archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert patches.applied_patches(root) == ()


def test_a_member_bigger_than_declared_is_refused_before_it_is_written(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER + b"trailing"})
    spec = make_spec(archive, after=AFTER)

    with pytest.raises(patches.PatchError):
        patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE


def test_content_that_does_not_match_the_pin_rolls_back(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": b"not what was pinned\n"})
    spec = make_spec(archive, after=AFTER)

    with pytest.raises(patches.PatchVerificationError):
        patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert list(root.glob("Data/.nadare-patch-*")) == []


def test_a_symlinked_target_is_refused(tmp_path):
    root = make_install(tmp_path)
    outside = tmp_path / "outside.r"
    outside.write_bytes(BEFORE)
    (root / "Data/table.r").unlink()
    (root / "Data/table.r").symlink_to(outside)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})

    with pytest.raises(patches.PatchError):
        patches.apply(root, make_spec(archive), archive)

    assert outside.read_bytes() == BEFORE


def test_a_symlinked_parent_that_escapes_the_install_is_refused(tmp_path):
    root = make_install(tmp_path)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (root / "linked").symlink_to(outside_dir)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive, rel="linked/table.r", pin_before=None)

    with pytest.raises(patches.PatchError):
        patches.apply(root, spec, archive)

    assert list(outside_dir.iterdir()) == []


def test_a_zip_slip_member_is_never_extracted(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(
        tmp_path,
        {"table_data.bytes": AFTER, "../../escape.r": b"pwned", "/abs.r": b"pwned"},
    )

    patches.apply(root, make_spec(archive), archive)

    assert not (tmp_path / "escape.r").exists()
    assert not Path("/abs.r").exists()


def test_without_a_before_pin_a_missing_file_is_created_and_removed_on_revert(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive, rel="Data/added.r", pin_before=None)
    (root / "Data/added.r").unlink(missing_ok=True)

    patches.apply(root, spec, archive)
    assert (root / "Data/added.r").read_bytes() == AFTER

    patches.revert(root, spec.id)
    assert not (root / "Data/added.r").exists()


# --------------------------------------------------------------------- revert


def test_revert_restores_the_original_bytes_and_mode(tmp_path):
    root = make_install(tmp_path)
    (root / "Data/table.r").chmod(0o600)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    patches.apply(root, spec, archive)

    patches.revert(root, spec.id)

    restored = root / "Data/table.r"
    assert restored.read_bytes() == BEFORE
    assert restored.stat().st_mode & 0o777 == 0o600
    assert patches.status(root, spec).state == "pristine"
    assert patches.applied_patches(root) == ()
    assert (
        json.loads((root / patches.STATE_DIR_NAME / patches.STATE_NAME).read_text())["patches"]
        == {}
    )


def test_reverting_a_patch_that_is_not_applied_is_an_error(tmp_path):
    root = make_install(tmp_path)
    with pytest.raises(patches.PatchError):
        patches.revert(root, "test-overlay")


def test_revert_refuses_when_the_backup_is_missing(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    record = patches.apply(root, spec, archive)
    (root / patches.STATE_DIR_NAME / str(record.files[0].backup)).unlink()

    with pytest.raises(patches.PatchError):
        patches.revert(root, spec.id)


def test_a_second_patch_cannot_take_over_a_file_another_one_owns(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    patches.apply(root, make_spec(archive), archive)
    other_archive = make_archive(tmp_path, {"m.bytes": b"third\n"}, name="other.zip")
    other = patches.PatchSpec(
        id="other-overlay",
        title="Other",
        version="v1",
        url="https://example.invalid/o.zip",
        archive_sha256=sha(other_archive.read_bytes()),
        files=(
            patches.PatchFile(
                member="m.bytes",
                dest="Data/table.r",
                sha256=sha(b"third\n"),
                size=6,
                before_sha256=sha(AFTER),
                before_size=len(AFTER),
            ),
        ),
    )

    with pytest.raises(patches.PatchConflict):
        patches.apply(root, other, other_archive)


# --------------------------------------------------------------------- crash recovery


def test_recover_undoes_an_interrupted_apply(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    record = patches.apply(root, spec, archive)

    # Recreate the state a crash between "file written" and "state saved" leaves behind.
    state_path = root / patches.STATE_DIR_NAME / patches.STATE_NAME
    state_path.unlink()
    patches._write_journal(root, spec.id, record.files, revert=False)

    assert patches.recover(root) == [spec.id]

    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert not state_path.exists() or json.loads(state_path.read_text())["patches"] == {}


def test_recover_does_nothing_without_a_journal(tmp_path):
    root = make_install(tmp_path)
    assert patches.recover(root) == []


def test_apply_refuses_while_an_unfinished_journal_exists(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    record_files = (
        patches.PatchFileState(
            dest="Data/table.r",
            member="table_data.bytes",
            post_sha256=sha(AFTER),
            post_size=len(AFTER),
            pre_sha256=sha(BEFORE),
            pre_size=len(BEFORE),
            backup="backups/test-overlay/Data/table.r",
        ),
    )
    (root / patches.STATE_DIR_NAME).mkdir(parents=True, exist_ok=True)
    patches._write_journal(root, "test-overlay", record_files, revert=False)

    with pytest.raises(patches.PatchError):
        patches.apply(root, spec, archive)


def test_the_install_lock_keeps_a_second_operation_out(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    with installlock.hold(root, patches.PatchError, "busy", busy=patches.PatchBusy):
        with pytest.raises(patches.PatchBusy):
            patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE


# --------------------------------------------------------------------- status


def test_status_distinguishes_pristine_applied_unknown_and_missing(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    assert patches.status(root, spec).state == "pristine"

    patches.apply(root, spec, archive)
    assert patches.status(root, spec).state == "applied"

    (root / "Data/table.r").write_bytes(b"edited by hand\n")
    edited = patches.status(root, spec)
    assert edited.state == "unknown"
    assert edited.files[0].sha256 == sha(b"edited by hand\n")

    (root / "Data/table.r").unlink()
    assert (
        patches.status(root, spec).state == "unknown"
        or patches.status(root, spec).files[0].state == "missing"
    )


def test_requirements_must_be_applied_first(tmp_path):
    # KiboTL's five containers travel in one spec, so it needs nothing else.
    spec = patchspecs.KIBOTL_EN
    assert patches.missing_requirements(spec, ()) == ()
    patches.ensure_requirements(spec, ())

    dependent = patches.PatchSpec(
        id="dependent-overlay",
        title="Depends on KiboTL",
        version="v1",
        url="https://example.invalid/d.zip",
        archive_sha256=sha(BEFORE),
        files=(patches.PatchFile(member="m", dest="Data/x.r", sha256=sha(AFTER), size=1),),
        requires=("kibotl-en",),
    )
    assert patches.missing_requirements(dependent, ()) == ("kibotl-en",)
    with pytest.raises(patches.PatchConflict):
        patches.ensure_requirements(dependent, ())


def test_a_directory_member_is_not_a_file(tmp_path):
    root = make_install(tmp_path)
    archive = tmp_path / "dir.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("table_data.bytes/", b"")
    spec = patches.PatchSpec(
        id="dir-overlay",
        title="t",
        version="v",
        url="https://example.invalid",
        archive_sha256=sha(archive.read_bytes()),
        files=(
            patches.PatchFile(
                member="table_data.bytes", dest="Data/table.r", sha256=sha(AFTER), size=len(AFTER)
            ),
        ),
    )

    with pytest.raises(patches.PatchError):
        patches.apply(root, spec, archive)


def test_a_journal_cannot_name_a_backup_outside_the_state_folder(tmp_path):
    root = make_install(tmp_path)
    (root / patches.STATE_DIR_NAME).mkdir(parents=True)
    patches._write_journal(
        root,
        "test-overlay",
        (
            patches.PatchFileState(
                dest="Data/table.r",
                member="table_data.bytes",
                post_sha256=sha(AFTER),
                post_size=len(AFTER),
                pre_sha256=sha(BEFORE),
                pre_size=len(BEFORE),
                backup="../../outside.r",
            ),
        ),
        revert=False,
    )

    with pytest.raises(patches.PatchError):
        patches.recover(root)

    assert (root / "Data/table.r").read_bytes() == BEFORE


def test_a_missing_backup_that_was_never_made_is_not_an_error(tmp_path):
    root = make_install(tmp_path)
    (root / patches.STATE_DIR_NAME).mkdir(parents=True)
    patches._write_journal(
        root,
        "test-overlay",
        (
            patches.PatchFileState(
                dest="Data/table.r",
                member="table_data.bytes",
                post_sha256=sha(AFTER),
                post_size=len(AFTER),
                pre_sha256=sha(BEFORE),
                pre_size=len(BEFORE),
                backup="backups/test-overlay/Data/table.r",
            ),
        ),
        revert=False,
    )

    assert patches.recover(root) == ["test-overlay"]
    assert (root / "Data/table.r").read_bytes() == BEFORE


def test_a_reflink_backup_is_used_when_the_filesystem_supports_it(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    record = patches.apply(root, make_spec(archive), archive)
    backup = root / patches.STATE_DIR_NAME / str(record.files[0].backup)

    # Whatever the filesystem did, the backup must be an independent copy: writing to it
    # must not change the patched file (a hardlink would fail this).
    backup.write_bytes(b"changed\n")
    assert (root / "Data/table.r").read_bytes() == AFTER
    assert os.stat(backup).st_ino != os.stat(root / "Data/table.r").st_ino


# --------------------------------------------------------------- after a game update (#1866)

NEW_BUILD = b"the next game build's container\n" * 36


def test_revert_after_a_game_update_keeps_the_new_files_and_forgets_the_patch(tmp_path):
    # The update rewrote the container: putting the old build's backup back would break the game.
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    patches.apply(root, spec, archive)
    (root / "Data/table.r").write_bytes(NEW_BUILD)

    patches.revert(root, spec.id)

    assert (root / "Data/table.r").read_bytes() == NEW_BUILD
    assert patches.applied_patches(root) == ()


def test_a_patch_undone_by_a_repair_can_be_applied_again(tmp_path):
    # Repair put the pristine file back but state.json still says "applied".
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    patches.apply(root, spec, archive)
    (root / "Data/table.r").write_bytes(BEFORE)

    patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == AFTER
    assert patches.status(root, spec).state == "applied"
    patches.revert(root, spec.id)
    assert (root / "Data/table.r").read_bytes() == BEFORE


def test_stale_lists_a_recorded_patch_the_game_files_no_longer_carry(tmp_path):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    assert patches.stale(root, (spec,)) == ()
    patches.apply(root, spec, archive)
    assert patches.stale(root, (spec,)) == ()
    (root / "Data/table.r").write_bytes(NEW_BUILD)
    assert patches.stale(root, (spec,)) == (spec,)


# ------------------------------------------------ guards found unguarded by the b27 mutation check


def test_content_of_the_right_size_but_other_bytes_is_refused(tmp_path):
    # Only the hash tells these apart: the size checks pass.
    root = make_install(tmp_path)
    forged = bytes(reversed(AFTER))
    assert len(forged) == len(AFTER) and forged != AFTER
    archive = make_archive(tmp_path, {"table_data.bytes": forged})
    spec = make_spec(archive, after=AFTER)

    with pytest.raises(patches.PatchVerificationError, match="does not match its pin"):
        patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert list(root.glob("Data/.nadare-patch-*")) == []


def test_a_backup_that_is_not_a_faithful_copy_stops_the_patch(tmp_path, monkeypatch):
    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})

    def bad_copy(source, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x" * len(BEFORE))  # right size, wrong bytes

    monkeypatch.setattr(patches, "_copy_with_reflink", bad_copy)
    with pytest.raises(patches.PatchVerificationError, match="faithful copy"):
        patches.apply(root, make_spec(archive), archive)

    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()


# --------------------------------------------------------------- fix1: rollback & orphan backups


def _two_file_install(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    orig1 = b"original file one\n" * 20
    orig2 = b"original file two\n" * 25
    root = tmp_path / "install"
    (root / "Data").mkdir(parents=True, exist_ok=True)
    (root / "Data/one.r").write_bytes(orig1)
    (root / "Data/two.r").write_bytes(orig2)
    return root, orig1, orig2


def _two_file_spec(
    archive: Path,
    orig1: bytes,
    orig2: bytes,
    after1: bytes,
    after2: bytes,
) -> patches.PatchSpec:
    return patches.PatchSpec(
        id="two-file-overlay",
        title="Two file overlay",
        version="v1",
        url="https://example.invalid/en.zip",
        archive_sha256=sha(archive.read_bytes()),
        files=(
            patches.PatchFile(
                member="one.bytes",
                dest="Data/one.r",
                sha256=sha(after1),
                size=len(after1),
                before_sha256=sha(orig1),
                before_size=len(orig1),
            ),
            patches.PatchFile(
                member="two.bytes",
                dest="Data/two.r",
                sha256=sha(after2),
                size=len(after2),
                before_sha256=sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )


def test_failed_apply_removes_its_backups_so_a_later_apply_succeeds(tmp_path):
    root, orig1, orig2 = _two_file_install(tmp_path)
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27

    # File 1 matches its pin and is written to disk; file 2 fails its member pin.
    bad_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": b"corrupted second file\n"},
        name="bad.zip",
    )
    bad_spec = _two_file_spec(bad_archive, orig1, orig2, after1, after2)

    with pytest.raises(patches.PatchVerificationError):
        patches.apply(root, bad_spec, bad_archive)

    assert (root / "Data/one.r").read_bytes() == orig1
    assert (root / "Data/two.r").read_bytes() == orig2
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    backup_dir = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / bad_spec.id
    assert not backup_dir.exists()

    # A subsequent good apply must succeed without FileExistsError.
    good_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": after2},
        name="good.zip",
    )
    good_spec = _two_file_spec(good_archive, orig1, orig2, after1, after2)
    patches.apply(root, good_spec, good_archive)

    assert (root / "Data/one.r").read_bytes() == after1
    assert (root / "Data/two.r").read_bytes() == after2
    assert patches.status(root, good_spec).state == "applied"


def test_failed_apply_keeps_backups_when_rollback_fails(tmp_path, monkeypatch):
    root, orig1, orig2 = _two_file_install(tmp_path)
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27
    bad_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": b"corrupted second file\n"},
        name="bad.zip",
    )
    bad_spec = _two_file_spec(bad_archive, orig1, orig2, after1, after2)

    orig_undo = patches._undo_entries

    def fail_undo(*args, **kwargs):
        raise patches.PatchError("rollback failed")

    monkeypatch.setattr(patches, "_undo_entries", fail_undo)
    with pytest.raises(patches.PatchRollbackError) as excinfo:
        patches.apply(root, bad_spec, bad_archive)

    assert "could not put every original file back" in str(excinfo.value).lower()
    assert isinstance(excinfo.value.__cause__, patches.PatchVerificationError)
    journal_path = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    assert journal_path.exists()
    backup_dir = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / bad_spec.id
    assert (backup_dir / "Data/one.r").read_bytes() == orig1
    assert (backup_dir / "Data/two.r").read_bytes() == orig2

    # Once the cause of the undo failure is gone, recover() uses the kept journal to restore.
    monkeypatch.setattr(patches, "_undo_entries", orig_undo)
    assert patches.recover(root) == [bad_spec.id]
    assert not journal_path.exists()
    assert (root / "Data/one.r").read_bytes() == orig1
    assert (root / "Data/two.r").read_bytes() == orig2


def test_copy_with_reflink_unlinks_partial_target_and_rollback_removes_backups(
    tmp_path, monkeypatch
):
    import errno
    import fcntl

    # Direct check: a copy that raises mid-way leaves no partial target file.
    src = tmp_path / "source.bin"
    payload = b"backup payload bytes\n" * 30
    src.write_bytes(payload)
    dst = tmp_path / "dest.bin"

    real_write = os.write

    def fail_ioctl(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "reflink not supported")

    def fail_mid_copy(fd, data):
        if payload[:16] in data:
            real_write(fd, data[:10])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(fcntl, "ioctl", fail_ioctl)
    monkeypatch.setattr(os, "write", fail_mid_copy)
    with pytest.raises(OSError, match="No space left on device"):
        patches._copy_with_reflink(src, dst)
    assert not dst.exists()

    # End-to-end in patches.apply(): file 1 backs up, file 2 fails mid-copy -> no partial backup,
    # rollback succeeds cleanly, and the backups folder is removed.
    root, orig1, orig2 = _two_file_install(tmp_path)
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27
    archive = make_archive(tmp_path, {"one.bytes": after1, "two.bytes": after2})
    spec = _two_file_spec(archive, orig1, orig2, after1, after2)

    def fail_second_backup(fd, data):
        if b"original file two" in data:
            real_write(fd, data[:10])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_second_backup)
    with pytest.raises(OSError, match="No space left on device"):
        patches.apply(root, spec, archive)

    backup_dir = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / spec.id
    assert not backup_dir.exists()
    assert not (root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME).exists()
    assert (root / "Data/one.r").read_bytes() == orig1
    assert (root / "Data/two.r").read_bytes() == orig2


def test_status_treats_removed_parent_folder_as_missing(tmp_path):
    import shutil

    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)
    shutil.rmtree(root / "Data")

    st = patches.status(root, spec)
    assert st.state == "unknown"
    assert st.files[0].state == "missing"


def test_apply_moves_orphan_backups_aside_and_keeps_their_bytes(tmp_path, caplog):
    import logging

    root = make_install(tmp_path)
    archive = make_archive(tmp_path, {"table_data.bytes": AFTER})
    spec = make_spec(archive)

    # Simulate leftover backups from an older Nadare (no state.json record, no journal).
    orphan_src = (
        root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / spec.id / "Data/table.r"
    )
    orphan_src.parent.mkdir(parents=True, exist_ok=True)
    stale_bytes = b"previous older-nadare backup bytes\n"
    orphan_src.write_bytes(stale_bytes)

    with caplog.at_level(logging.WARNING, logger="nadare.core.patches"):
        patches.apply(root, spec, archive)

    assert (root / "Data/table.r").read_bytes() == AFTER
    backups_root = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME
    orphans = sorted(backups_root.glob(f"{spec.id}.orphan-*"))
    assert len(orphans) == 1
    assert (orphans[0] / "Data/table.r").read_bytes() == stale_bytes
    assert (backups_root / spec.id / "Data/table.r").read_bytes() == BEFORE
    assert any("orphan" in rec.message.lower() for rec in caplog.records)


# --------------------------------------------------------------- fix3: selective undo & removed dir


def test_recover_after_failed_rollback_and_repair_skips_corrupt_backup_and_clears_journal(
    tmp_path, monkeypatch, caplog
):
    import logging

    root, orig1, orig2 = _two_file_install(tmp_path)
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27
    bad_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": b"corrupted second file\n"},
        name="bad.zip",
    )
    bad_spec = _two_file_spec(bad_archive, orig1, orig2, after1, after2)

    real_undo = patches._undo_entries
    monkeypatch.setattr(
        patches, "_undo_entries", lambda *a, **k: (_ for _ in ()).throw(patches.PatchError("boom"))
    )
    with pytest.raises(patches.PatchRollbackError):
        patches.apply(root, bad_spec, bad_archive)

    journal_path = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    backup_dir = root / patches.STATE_DIR_NAME / patches.BACKUP_DIR_NAME / bad_spec.id
    assert journal_path.exists()
    journal_data = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal_data["files"][0]["post_size"] == len(after1)
    assert (root / "Data/one.r").read_bytes() == after1
    assert (root / "Data/two.r").read_bytes() == orig2

    # Corrupt both backups, then simulate Repair restoring the pristine game files.
    (backup_dir / "Data/one.r").write_bytes(b"corrupt backup one\n")
    (backup_dir / "Data/two.r").write_bytes(b"corrupt backup two\n")
    (root / "Data/one.r").write_bytes(orig1)
    (root / "Data/two.r").write_bytes(orig2)

    monkeypatch.setattr(patches, "_undo_entries", real_undo)
    with caplog.at_level(logging.INFO, logger="nadare.core.patches"):
        assert patches.recover(root) == [bad_spec.id]
    assert not any("rewritten" in rec.message.lower() for rec in caplog.records)
    assert not journal_path.exists()
    assert (root / "Data/one.r").read_bytes() == orig1
    assert (root / "Data/two.r").read_bytes() == orig2

    # A subsequent good apply now succeeds cleanly.
    good_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": after2},
        name="good.zip",
    )
    good_spec = _two_file_spec(good_archive, orig1, orig2, after1, after2)
    patches.apply(root, good_spec, good_archive)
    assert (root / "Data/one.r").read_bytes() == after1
    assert (root / "Data/two.r").read_bytes() == after2


def test_recover_after_failed_rollback_and_game_update_keeps_new_build(
    tmp_path, monkeypatch, caplog
):
    import logging

    root, orig1, orig2 = _two_file_install(tmp_path)
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27
    bad_archive = make_archive(
        tmp_path,
        {"one.bytes": after1, "two.bytes": b"corrupted second file\n"},
        name="bad.zip",
    )
    bad_spec = _two_file_spec(bad_archive, orig1, orig2, after1, after2)

    real_undo = patches._undo_entries
    monkeypatch.setattr(
        patches, "_undo_entries", lambda *a, **k: (_ for _ in ()).throw(patches.PatchError("boom"))
    )
    with pytest.raises(patches.PatchRollbackError):
        patches.apply(root, bad_spec, bad_archive)

    journal_path = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    assert journal_path.exists()

    # Game update rewrites file 1 to NEW_BUILD and removes file 2.
    (root / "Data/one.r").write_bytes(NEW_BUILD)
    (root / "Data/two.r").unlink()

    monkeypatch.setattr(patches, "_undo_entries", real_undo)
    with caplog.at_level(logging.INFO, logger="nadare.core.patches"):
        assert patches.recover(root) == [bad_spec.id]

    assert not journal_path.exists()
    assert (root / "Data/one.r").read_bytes() == NEW_BUILD
    assert not (root / "Data/two.r").exists()
    assert any("Data/one.r" in rec.message for rec in caplog.records)
    assert any("Data/two.r" in rec.message for rec in caplog.records)

    # Also verify a removed parent folder does not crash recover() or recreate the folder.
    import shutil

    patches._write_journal(
        root,
        bad_spec.id,
        (
            patches.PatchFileState(
                dest="Data/one.r",
                member="one.bytes",
                post_sha256=sha(after1),
                post_size=len(after1),
                pre_sha256=sha(orig1),
                pre_size=len(orig1),
                backup=f"backups/{bad_spec.id}/Data/one.r",
            ),
        ),
        revert=False,
    )
    shutil.rmtree(root / "Data")
    assert patches.recover(root) == [bad_spec.id]
    assert not journal_path.exists()
    assert not (root / "Data").exists()


def test_recover_without_pre_unlinks_only_when_target_holds_post_bytes(tmp_path):
    root = make_install(tmp_path)
    added = root / "Data/added.r"
    entry = patches.PatchFileState(
        dest="Data/added.r",
        member="added.bytes",
        post_sha256=sha(AFTER),
        post_size=len(AFTER),
        pre_sha256=None,
        pre_size=None,
        backup=None,
    )
    (root / patches.STATE_DIR_NAME).mkdir(parents=True, exist_ok=True)

    # Holding post bytes -> unlinked.
    added.write_bytes(AFTER)
    patches._write_journal(root, "added-overlay", (entry,), revert=False)
    assert patches.recover(root) == ["added-overlay"]
    assert not added.exists()

    # Holding another build's bytes -> left untouched.
    added.write_bytes(NEW_BUILD)
    patches._write_journal(root, "added-overlay", (entry,), revert=False)
    assert patches.recover(root) == ["added-overlay"]
    assert added.read_bytes() == NEW_BUILD


def test_recover_legacy_journal_without_post_sha256_keeps_old_behaviour(tmp_path):
    root = make_install(tmp_path)
    backup = root / patches.STATE_DIR_NAME / "backups/legacy/Data/table.r"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(BEFORE)
    (root / "Data/table.r").write_bytes(NEW_BUILD)
    (root / "Data/added.r").write_bytes(NEW_BUILD)

    # Older journal format: no post_sha256 field.
    journal = root / patches.STATE_DIR_NAME / patches.JOURNAL_NAME
    journal.write_text(
        json.dumps(
            {
                "version": patches.STATE_VERSION,
                "patch": "legacy",
                "reverting": False,
                "files": [
                    {
                        "dest": "Data/table.r",
                        "backup": "backups/legacy/Data/table.r",
                        "pre_sha256": sha(BEFORE),
                        "pre_size": len(BEFORE),
                        "mode": 0o644,
                    },
                    {
                        "dest": "Data/added.r",
                        "backup": None,
                        "pre_sha256": None,
                        "pre_size": None,
                        "mode": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert patches.recover(root) == ["legacy"]
    assert not journal.exists()
    assert (root / "Data/table.r").read_bytes() == BEFORE
    assert not (root / "Data/added.r").exists()


def test_still_patched_revert_and_stale_handle_removed_parent_folder(tmp_path):
    import shutil

    root = tmp_path / "install"
    orig1 = b"original file one\n" * 20
    orig2 = b"original file two\n" * 25
    after1 = b"patched file one\n" * 22
    after2 = b"patched file two\n" * 27
    (root / "Data").mkdir(parents=True)
    (root / "Sub").mkdir(parents=True)
    (root / "Data/one.r").write_bytes(orig1)
    (root / "Sub/two.r").write_bytes(orig2)

    archive = make_archive(tmp_path, {"one.bytes": after1, "two.bytes": after2})
    spec = patches.PatchSpec(
        id="two-dir-overlay",
        title="Two dir overlay",
        version="v1",
        url="https://example.invalid/en.zip",
        archive_sha256=sha(archive.read_bytes()),
        files=(
            patches.PatchFile(
                member="one.bytes",
                dest="Data/one.r",
                sha256=sha(after1),
                size=len(after1),
                before_sha256=sha(orig1),
                before_size=len(orig1),
            ),
            patches.PatchFile(
                member="two.bytes",
                dest="Sub/two.r",
                sha256=sha(after2),
                size=len(after2),
                before_sha256=sha(orig2),
                before_size=len(orig2),
            ),
        ),
    )
    record = patches.apply(root, spec, archive)

    shutil.rmtree(root / "Sub")
    assert patches._still_patched(root, record.files[1]) is False
    assert patches.stale(root, (spec,)) == (spec,)

    patches.revert(root, spec.id, keep_backups=False)
    assert patches.applied_patches(root) == ()
    assert (root / "Data/one.r").read_bytes() == orig1
    assert not (root / "Sub").exists()
