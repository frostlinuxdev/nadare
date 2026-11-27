# SPDX-License-Identifier: GPL-3.0-or-later
"""Applying and removing a declared overlay from a game's page or the CLI (#1866)."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.core import backends, download, operation_policy, patches, patchops, patchspecs
from nadare.gui.workers import BackendWorker

BEFORE = b"japanese container\n" * 40
AFTER = b"english container\n" * 48
KIND = "patchops-test"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeBackend(backends.GameBackend):
    kind = KIND

    def __init__(self, ctx, root: Path) -> None:
        super().__init__(ctx)
        self.root = root
        self.repaired = False

    def install_dir(self) -> Path:
        return self.root

    def status(self):
        return backends.Status("installed")

    def install(self):
        pass

    def update(self):
        (self.root / "Data/table.r").write_bytes(b"next build\n" * 50)

    def repair(self):
        (self.root / "Data/table.r").write_bytes(BEFORE)

    def launch(self):
        return 0

    def stop(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "install"
    (root / "Data").mkdir(parents=True)
    (root / "Data/table.r").write_bytes(BEFORE)
    source = tmp_path / "upstream.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("table_data.bytes", AFTER)
    spec = patches.PatchSpec(
        id="test-en",
        title="Test English",
        version="v1-123",
        url="https://example.invalid/en.zip",
        archive_sha256=sha(source.read_bytes()),
        files=(
            patches.PatchFile(
                member="table_data.bytes",
                dest="Data/table.r",
                sha256=sha(AFTER),
                size=len(AFTER),
                before_sha256=sha(BEFORE),
                before_size=len(BEFORE),
            ),
        ),
    )
    monkeypatch.setitem(patchspecs.SPECS, KIND, (spec,))
    fetched = []

    def fake_download(url, dest, *, expected_sha256=None, **kwargs):
        fetched.append((url, expected_sha256))
        assert expected_sha256 == spec.archive_sha256
        shutil.copyfile(source, dest)

    monkeypatch.setattr(download, "download", fake_download)
    monkeypatch.setitem(backends._REGISTRY, KIND, FakeBackend)
    ctx = make_context(tmp_path, KIND)
    events = []
    ctx.emit = events.append
    return FakeBackend(ctx, root), spec, fetched, events


def test_only_a_backend_with_specs_offers_patches(setup):
    backend, spec, _, _ = setup
    assert patchops.offered(backend) == (spec,)
    assert patchspecs.specs_for("something-else") == ()


def test_the_azur_jp_backend_offers_the_kibotl_overlay():
    assert patchspecs.specs_for("azurpromilia-jp-yostar") == (patchspecs.KIBOTL_EN,)


def test_apply_downloads_the_pinned_archive_and_applies_it(setup):
    backend, spec, fetched, _ = setup
    patchops.apply(backend, spec.id)
    assert fetched == [(spec.url, spec.archive_sha256)]
    assert (backend.root / "Data/table.r").read_bytes() == AFTER
    assert patchops.state(backend, spec.id) == "applied"


def test_apply_refuses_another_build_before_downloading_anything(setup):
    backend, spec, fetched, _ = setup
    (backend.root / "Data/table.r").write_bytes(b"other build\n" * 9)
    with pytest.raises(backends.BackendError, match="build"):
        patchops.apply(backend, spec.id)
    assert fetched == []
    assert (backend.root / "Data/table.r").read_bytes() == b"other build\n" * 9


def test_apply_reuses_a_cached_archive(setup):
    backend, spec, fetched, _ = setup
    patchops.apply(backend, spec.id)
    patchops.remove(backend, spec.id)
    patchops.apply(backend, spec.id)
    assert len(fetched) == 1


def test_remove_restores_the_original_file(setup):
    backend, spec, _, _ = setup
    patchops.apply(backend, spec.id)
    patchops.remove(backend, spec.id)
    assert (backend.root / "Data/table.r").read_bytes() == BEFORE
    assert patchops.state(backend, spec.id) == "pristine"


def test_an_unknown_patch_id_is_a_clear_error(setup):
    backend, _, _, _ = setup
    with pytest.raises(backends.BackendError, match="no-such"):
        patchops.apply(backend, "no-such")


def test_an_interrupted_operation_is_rolled_back_first(setup, monkeypatch):
    backend, spec, _, _ = setup
    calls = []
    monkeypatch.setattr(patches, "recover", lambda root: calls.append(root) or [])
    patchops.apply(backend, spec.id)
    assert calls == [backend.root]


def test_a_repair_that_undid_the_patch_is_announced(setup):
    backend, spec, _, events = setup
    patchops.apply(backend, spec.id)
    backend.repair()
    events.clear()
    assert patchops.warn_if_undone(backend) is True
    text = " ".join(event.message for event in events)
    assert "English" in text or spec.title in text
    assert "again" in text


def test_nothing_is_said_when_the_patch_survived_or_was_never_applied(setup):
    backend, spec, _, events = setup
    assert patchops.warn_if_undone(backend) is False
    patchops.apply(backend, spec.id)
    assert patchops.warn_if_undone(backend) is False


def test_the_operations_hold_the_install_lock_and_reprobe():
    for name in ("apply_patch", "remove_patch"):
        policy = operation_policy.policy_for(name)
        assert policy.locked and policy.mutating and policy.reprobe


@pytest.mark.parametrize("operation", ["apply_patch", "remove_patch"])
def test_the_worker_runs_the_patch_operations(setup, operation):
    backend, spec, _, _ = setup
    if operation == "remove_patch":
        patchops.apply(backend, spec.id)
    worker = BackendWorker(backend.ctx.config, backend.ctx.name, operation, payload=spec.id)
    assert worker._dispatch(backend) == 0
    expected = "applied" if operation == "apply_patch" else "pristine"
    assert patchops.state(backend, spec.id) == expected


@pytest.mark.parametrize("operation", ["repair", "update"])
def test_the_worker_warns_when_repair_or_update_undid_the_patch(setup, operation):
    backend, spec, _, events = setup
    patchops.apply(backend, spec.id)
    events.clear()
    worker = BackendWorker(backend.ctx.config, backend.ctx.name, operation)
    worker._dispatch(backend)
    assert any("again" in event.message for event in events)


# ------------------------------------------------------------------------------- CLI


def _cli(tmp_path, monkeypatch, backend):
    from nadare import app
    from nadare.core.config import Config

    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("azur", backend="wine", title="Azur")
    config.save()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    return lambda *argv: app.run_cli(["-c", str(path), "patch", *argv, "-p", "azur"])


def test_the_cli_applies_reports_and_removes(setup, tmp_path, monkeypatch, capsys):
    backend, spec, _, _ = setup
    run = _cli(tmp_path, monkeypatch, backend)
    assert run("status") == 0
    assert "pristine" in capsys.readouterr().out
    assert run("apply") == 0
    assert patchops.state(backend, spec.id) == "applied"
    assert run("status") == 0
    assert "applied" in capsys.readouterr().out
    assert run("remove") == 0
    assert patchops.state(backend, spec.id) == "pristine"


def test_the_cli_says_when_a_game_has_no_patches(tmp_path, monkeypatch, capsys):
    from _backend_helpers import make_context as ctx_for

    backend = FakeBackend(ctx_for(tmp_path, "no-patches"), tmp_path)
    backend.kind = "no-patches"
    run = _cli(tmp_path, monkeypatch, backend)
    assert run("status") == 1
    assert "no patches" in capsys.readouterr().err


def test_a_patch_applied_by_hand_is_reported_not_fought(setup):
    # The install already carries the overlay's files but Nadare has no record (no backups).
    backend, spec, fetched, events = setup
    (backend.root / "Data/table.r").write_bytes(AFTER)
    patchops.apply(backend, spec.id)
    assert fetched == []
    assert any("outside Nadare" in event.message for event in events)
    with pytest.raises(backends.BackendError, match="Repair files"):
        patchops.remove(backend, spec.id)
    assert (backend.root / "Data/table.r").read_bytes() == AFTER


def test_an_archive_changed_upstream_gets_a_plain_message(setup, monkeypatch):
    # KiboTL re-uploaded en.zip under the same tag (2026-11-26): the pin no longer matches.
    backend, spec, _, _ = setup

    def changed(url, dest, **kwargs):
        raise download.IntegrityError(f"sha256 mismatch for {dest.name}")

    monkeypatch.setattr(download, "download", changed)
    with pytest.raises(backends.BackendError) as caught:
        patchops.apply(backend, spec.id)
    message = str(caught.value)
    assert "changed" in message and "later" in message
    assert "Nadare update" not in message
    assert "sha256" not in message
    assert (backend.root / "Data/table.r").read_bytes() == BEFORE
