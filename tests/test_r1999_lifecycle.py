# SPDX-License-Identifier: GPL-3.0-or-later
"""Real archive lifecycle regression for full and incremental packages."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.backends import BackendError
from nadare.core.sources import r1999


def _package(tmp_path, blobs, label, files):
    """A real 7z package of ``files`` whose URL carries its exact size and MD5."""
    folder = tmp_path / label
    folder.mkdir()
    for name, data in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    archive = tmp_path / (label + ".7z")
    subprocess.run(
        ["7z", "a", "-t7z", "-m0=lzma2", str(archive), "."],
        cwd=folder,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    blob = archive.read_bytes()
    url = (
        f"https://download.bluepoch.com/en/game/{label}_size_{len(blob)}"
        f"_md5_{hashlib.md5(blob).hexdigest()}_0.7z"
    )
    blobs[url] = blob
    return {
        "packageSize": len(blob),
        "packageRealSize": sum(map(len, files.values())),
        "packageUrls": [url],
    }


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    blobs = {}

    def package(label, files):
        return _package(tmp_path, blobs, label, files)

    def plan(version, files, *, current="", full=False):
        resource = package(version, files)

        def post(url, body, headers):
            data = (
                {"latestVersion": version}
                if url.endswith("/version")
                else {
                    "fullPackage": resource if full or not current else None,
                    "diffPackage": resource if current and not full else None,
                }
            )
            return json.dumps({"code": 200, "data": data}).encode()

        return r1999.resolve(post, current_version=current)

    def download(url, path, **kwargs):
        assert kwargs["redirect_allowed"](url)
        assert kwargs["expected_size"] == len(blobs[url])
        Path(path).write_bytes(blobs[url])

    monkeypatch.setattr(dd, "download", download)
    return plan, tmp_path / "install"


class Publisher:
    """The live service's three answers: a full package, one delta, and null when current."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.calls = []
        self.base = None
        self.delta = None
        self.full = None

    def post(self, url, body, headers):
        request = json.loads(body)
        self.calls.append((url, request))
        if url.endswith("/version"):
            data = {"latestVersion": "3.9.1"}
        elif request["currentVersion"] == "":
            data = {"fullPackage": self.full, "diffPackage": None}
        elif request["currentVersion"] == "3.9.0":
            data = {"fullPackage": None, "diffPackage": self.delta}
        else:
            data = None  # the saved live reply for an up-to-date client
        return json.dumps({"code": 200, "data": data}).encode()

    def download(self, url, path, **kwargs):
        assert kwargs["redirect_allowed"](url)
        assert kwargs["expected_size"] == len(self.blobs[url])
        Path(path).write_bytes(self.blobs[url])

    def plan(self, version, package, *, current=""):
        """Build one plan directly, for setting up installs before a repair."""

        def post(url, body, headers):
            data = {"latestVersion": version} if url.endswith("/version") else package
            return json.dumps({"code": 200, "data": data}).encode()

        return r1999.resolve(post, current_version=current)


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    blobs = {}
    pub = Publisher(blobs)
    pub.base = _package(
        tmp_path,
        blobs,
        "full-3.9.0",
        {"keep.bin": b"base keep", "updated.bin": b"old", "retired.bin": b"base"},
    )
    pub.delta = _package(
        tmp_path,
        blobs,
        "delta-3.9.0-to-3.9.1",
        {"updated.bin": b"new", "retired.bin": b"delta"},
    )
    pub.full = _package(
        tmp_path,
        blobs,
        "full-3.9.1",
        {"keep.bin": b"base keep", "updated.bin": b"new"},
    )
    monkeypatch.setattr(dd, "download", pub.download)
    monkeypatch.setattr(r1999, "default_post", pub.post)
    return pub


def _backend(tmp_path):
    from _backend_helpers import make_context

    from nadare.backends.direct import R1999Backend

    return R1999Backend(
        make_context(tmp_path, "direct-r1999", {"prefix": str(tmp_path / "pfx")})
    )


def _delta_install(publisher, backend):
    """A finished 3.9.1 install reached through 3.9.0 full + one delta."""
    install = backend.install_dir()
    dd.sync(
        publisher.plan("3.9.0", {"fullPackage": publisher.base, "diffPackage": None}),
        install,
        workers=1,
    )
    dd.sync(
        publisher.plan(
            "3.9.1",
            {"fullPackage": None, "diffPackage": publisher.delta},
            current="3.9.0",
        ),
        install,
        workers=1,
    )
    backend._write_version_file("3.9.1", "r1999")
    assert (install / "updated.bin").read_bytes() == b"new"
    return install


def test_full_delta_delta_preserves_members_and_receipts(lifecycle):
    plan, install = lifecycle
    first = plan("3.9.0", {"unchanged.bin": b"keep me", "updated.bin": b"old"})
    dd.sync(first, install, workers=1)
    full_receipt = (install / dd.STATE_DIR / "package-00.7z.done").read_bytes()
    delta = plan("3.9.1", {"updated.bin": b"new", "added.bin": b"first delta"}, current="3.9.0")
    assert delta.archives[0].name == "delta-3.9.0-to-3.9.1-package-00.7z"
    dd.sync(delta, install, workers=1)
    assert (install / "unchanged.bin").read_bytes() == b"keep me"
    assert (install / dd.STATE_DIR / "package-00.7z.done").read_bytes() == full_receipt
    delta_receipt = install / dd.STATE_DIR / (delta.archives[0].name + ".done")
    saved_delta = delta_receipt.read_bytes()
    second = plan("3.9.2", {"updated.bin": b"newer"}, current="3.9.1")
    dd.sync(second, install, workers=1)
    assert (install / "updated.bin").read_bytes() == b"newer"
    assert (install / "unchanged.bin").read_bytes() == b"keep me"
    assert (install / "added.bin").read_bytes() == b"first delta"
    assert delta_receipt.read_bytes() == saved_delta
    assert len(list((install / dd.STATE_DIR).glob("*.done"))) == 3


def test_full_fallback_replaces_base_and_delta(lifecycle):
    plan, install = lifecycle
    dd.sync(
        plan("3.9.0", {"old.bin": b"old", "updated.bin": b"old", "retired.bin": b"base"}),
        install,
        workers=1,
    )
    dd.sync(
        plan(
            "3.9.1",
            {"added.bin": b"delta", "updated.bin": b"new", "retired.bin": b"updated"},
            current="3.9.0",
        ),
        install,
        workers=1,
    )
    replacement = plan("4.0.0", {"updated.bin": b"full replacement"}, current="3.9.1", full=True)
    dd.sync(replacement, install, workers=1)
    assert (install / "updated.bin").read_bytes() == b"full replacement"
    assert not (install / "old.bin").exists()
    assert not (install / "added.bin").exists()
    assert not (install / "retired.bin").exists()
    assert [p.name for p in (install / dd.STATE_DIR).glob("*.done")] == ["package-00.7z.done"]
    assert dd.plan_missing(replacement, install, verify=True) == ([], [])


def test_equal_receipt_mtimes_keep_delta_ownership(lifecycle):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"retired.bin": b"base", "updated.bin": b"old"}), install, workers=1)
    delta = plan(
        "3.9.1", {"retired.bin": b"delta", "updated.bin": b"new"}, current="3.9.0"
    )
    dd.sync(delta, install, workers=1)
    state = install / dd.STATE_DIR
    base_record = state / "package-00.7z.done"
    delta_record = state / (delta.archives[0].name + ".done")
    # Coarse timestamps (FAT/ext3/NFS, copied trees) give both receipts the same mtime.
    stamp = 1_700_000_000_000_000_000
    os.utime(base_record, ns=(stamp, stamp))
    os.utime(delta_record, ns=(stamp, stamp))
    replacement = plan("4.0.0", {"updated.bin": b"full"}, current="3.9.1", full=True)
    dd.sync(replacement, install, workers=1)
    assert not (install / "retired.bin").exists()
    assert (install / "updated.bin").read_bytes() == b"full"


def test_receipt_apply_order_beats_a_newer_base_mtime(lifecycle):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"retired.bin": b"base", "updated.bin": b"old"}), install, workers=1)
    delta = plan(
        "3.9.1", {"retired.bin": b"delta", "updated.bin": b"new"}, current="3.9.0"
    )
    dd.sync(delta, install, workers=1)
    state = install / dd.STATE_DIR
    base_record = state / "package-00.7z.done"
    delta_record = state / (delta.archives[0].name + ".done")
    # Clock skew or a copied tree can give the older receipt the newer mtime.
    stamp = 1_700_000_000_000_000_000
    os.utime(base_record, ns=(stamp + 10_000_000_000, stamp + 10_000_000_000))
    os.utime(delta_record, ns=(stamp, stamp))
    replacement = plan("4.0.0", {"updated.bin": b"full"}, current="3.9.1", full=True)
    dd.sync(replacement, install, workers=1)
    assert not (install / "retired.bin").exists()


def test_second_delta_ownership_beats_a_reversed_mtime(lifecycle):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"retired.bin": b"base", "updated.bin": b"old"}), install, workers=1)
    first = plan("3.9.1", {"retired.bin": b"one", "updated.bin": b"new"}, current="3.9.0")
    dd.sync(first, install, workers=1)
    second = plan("3.9.2", {"retired.bin": b"two"}, current="3.9.1")
    dd.sync(second, install, workers=1)
    assert (install / "retired.bin").read_bytes() == b"two"
    state = install / dd.STATE_DIR
    first_record = state / (first.archives[0].name + ".done")
    second_record = state / (second.archives[0].name + ".done")
    stamp = 1_700_000_000_000_000_000
    os.utime(first_record, ns=(stamp + 10_000_000_000, stamp + 10_000_000_000))
    os.utime(second_record, ns=(stamp, stamp))
    replacement = plan("4.0.0", {"updated.bin": b"full"}, current="3.9.2", full=True)
    dd.sync(replacement, install, workers=1)
    assert not (install / "retired.bin").exists()


def test_old_receipt_without_sequence_uses_the_additive_flag(lifecycle):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"retired.bin": b"base", "updated.bin": b"old"}), install, workers=1)
    delta = plan(
        "3.9.1", {"retired.bin": b"delta", "updated.bin": b"new"}, current="3.9.0"
    )
    dd.sync(delta, install, workers=1)
    state = install / dd.STATE_DIR
    base_record = state / "package-00.7z.done"
    delta_record = state / (delta.archives[0].name + ".done")
    # A receipt between releases has the additive flag but no sequence yet.
    for path in (base_record, delta_record):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("sequence", None)
        path.write_text(json.dumps(record), encoding="utf-8")
    stamp = 1_700_000_000_000_000_000
    os.utime(base_record, ns=(stamp, stamp))
    os.utime(delta_record, ns=(stamp, stamp))
    replacement = plan("4.0.0", {"updated.bin": b"full"}, current="3.9.1", full=True)
    dd.sync(replacement, install, workers=1)
    assert not (install / "retired.bin").exists()


def test_old_receipts_without_order_fields_still_work(lifecycle):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"retired.bin": b"base", "updated.bin": b"old"}), install, workers=1)
    delta = plan(
        "3.9.1", {"retired.bin": b"delta", "updated.bin": b"new"}, current="3.9.0"
    )
    dd.sync(delta, install, workers=1)
    state = install / dd.STATE_DIR
    base_record = state / "package-00.7z.done"
    delta_record = state / (delta.archives[0].name + ".done")
    # Receipts written by older Nadare releases have neither field: mtime still orders them.
    for path in (base_record, delta_record):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("sequence", None)
        record.pop("additive", None)
        path.write_text(json.dumps(record), encoding="utf-8")
    stamp = 1_700_000_000_000_000_000
    os.utime(base_record, ns=(stamp, stamp))
    os.utime(delta_record, ns=(stamp + 10_000_000_000, stamp + 10_000_000_000))
    replacement = plan("4.0.0", {"updated.bin": b"full"}, current="3.9.1", full=True)
    dd.sync(replacement, install, workers=1)
    assert not (install / "retired.bin").exists()


def test_full_replacement_keeps_user_and_foreign_files(lifecycle, tmp_path):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"shared.bin": b"shared", "old.bin": b"old"}), install, workers=1)
    (install / "notes.txt").write_bytes(b"mine")
    (install / "old.bin").write_bytes(b"new")  # same size, different bytes
    state = install / dd.STATE_DIR
    # Another game and another slot of this game still own their own files.
    for name, record in (
        ("other-game.done", {"game": "other", "members": ["shared.bin"]}),
        ("dlc.done", {"game": "r1999", "slot": "dlc", "members": ["shared.bin"]}),
    ):
        record.update(
            {
                "version": "1.0.0",
                "url": "https://download.example.invalid/pkg.7z",
                "member_sizes": {"shared.bin": len(b"shared")},
                "member_crc32": {"shared.bin": _crc(b"shared")},
            }
        )
        (state / name).write_text(json.dumps(record), encoding="utf-8")
    dd.sync(
        plan("4.0.0", {"replacement.bin": b"new"}, current="3.9.1", full=True),
        install,
        workers=1,
    )
    assert (install / "notes.txt").read_bytes() == b"mine"
    assert (install / "old.bin").read_bytes() == b"new"
    assert (install / "shared.bin").read_bytes() == b"shared"
    assert (install / "replacement.bin").read_bytes() == b"new"


def test_full_replacement_does_not_follow_a_symlink_at_a_retired_path(lifecycle, tmp_path):
    plan, install = lifecycle
    dd.sync(plan("3.9.0", {"old.bin": b"old"}), install, workers=1)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"old")  # same bytes as the retired file, so only the link type saves it
    (install / "old.bin").unlink()
    (install / "old.bin").symlink_to(outside)
    dd.sync(
        plan("4.0.0", {"replacement.bin": b"new"}, current="3.9.1", full=True),
        install,
        workers=1,
    )
    assert outside.read_bytes() == b"old"
    assert (install / "old.bin").is_symlink()
    assert (install / "replacement.bin").read_bytes() == b"new"


def _crc(data):
    import zlib

    return f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"


def test_repair_from_outdated_install_fetches_the_full_package(publisher, tmp_path):
    backend = _backend(tmp_path)
    install = backend.install_dir()
    dd.sync(
        publisher.plan("3.9.0", {"fullPackage": publisher.base, "diffPackage": None}),
        install,
        workers=1,
    )
    backend._write_version_file("3.9.0", "r1999")
    (install / "keep.bin").write_bytes(b"damaged")
    publisher.calls.clear()

    backend.repair()

    assert (install / "keep.bin").read_bytes() == b"base keep"
    assert (install / "updated.bin").read_bytes() == b"new"
    resources = [body for url, body in publisher.calls if url.endswith("/resource")]
    assert resources and all(body["currentVersion"] == "" for body in resources)


def test_repair_on_an_up_to_date_delta_install_fetches_the_full_package(publisher, tmp_path):
    backend = _backend(tmp_path)
    install = _delta_install(publisher, backend)
    (install / "keep.bin").write_bytes(b"damaged")
    (install / "updated.bin").write_bytes(b"damaged")
    publisher.calls.clear()

    backend.repair()

    assert (install / "keep.bin").read_bytes() == b"base keep"
    assert (install / "updated.bin").read_bytes() == b"new"
    resources = [body for url, body in publisher.calls if url.endswith("/resource")]
    assert resources and all(body["currentVersion"] == "" for body in resources)


def test_status_offers_repair_for_an_installed_game(publisher, tmp_path):
    backend = _backend(tmp_path)
    _delta_install(publisher, backend)
    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    assert backend.status().data["can_repair"] is True


def test_check_update_at_latest_skips_resource_and_refreshes_cache(publisher, tmp_path):
    backend = _backend(tmp_path)
    _delta_install(publisher, backend)
    latest = backend._latest_path()
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({"version": "3.9.1", "checked_at": 0}), encoding="utf-8")
    publisher.calls.clear()

    backend.check_update()

    assert [url for url, _ in publisher.calls if url.endswith("/resource")] == []
    assert [url for url, _ in publisher.calls if url.endswith("/version")]
    assert backend._read_latest()["checked_at"] > 0


def test_update_at_latest_downloads_nothing_and_refreshes_cache(publisher, tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    install = _delta_install(publisher, backend)
    (install / r1999.EXE).write_bytes(b"game")
    latest = backend._latest_path()
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({"version": "3.9.1", "checked_at": 0}), encoding="utf-8")
    receipts = {path.name: path.read_bytes() for path in (install / dd.STATE_DIR).glob("*.done")}
    publisher.calls.clear()
    downloads = []

    def download(*args, **kwargs):
        downloads.append(args)

    monkeypatch.setattr(dd, "download", download)
    backend.update()

    assert downloads == []
    assert [url for url, _ in publisher.calls if url.endswith("/resource")] == []
    assert backend._read_latest()["checked_at"] > 0
    assert backend._read_version_file()["version"] == "3.9.1"
    after = {path.name: path.read_bytes() for path in (install / dd.STATE_DIR).glob("*.done")}
    assert after == receipts


def test_verify_explains_limit_without_fetching_full_plan(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.direct import R1999Backend

    backend = R1999Backend(
        make_context(tmp_path, "direct-r1999", {"prefix": str(tmp_path / "pfx")})
    )
    backend._write_version_file("3.9.2", "r1999")

    def unexpected_plan(*args, **kwargs):
        pytest.fail("Verify must not fetch a full install plan")

    monkeypatch.setattr(r1999, "default_post", unexpected_plan)
    with pytest.raises(BackendError, match="Verification is unavailable.*complete file list"):
        backend._missing_entries()

    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    target = backend.install_dir() / r1999.EXE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"game")
    monkeypatch.setattr(backend, "direct_target", lambda: target)
    monkeypatch.setattr(backend, "runner_id", lambda: "")
    result = backend.verify()
    assert result["verified"] is True
    assert "files" not in result["checks"]
    assert "Verification is unavailable" in result["message"]


def test_repair_restores_a_same_size_damaged_file_on_a_full_install(publisher, tmp_path):
    # A damaged file that kept its size must still be found: Repair may not trust receipts alone.
    backend = _backend(tmp_path)
    install = backend.install_dir()
    dd.sync(
        publisher.plan("3.9.1", {"fullPackage": publisher.full, "diffPackage": None}),
        install,
        workers=1,
    )
    backend._write_version_file("3.9.1", "r1999")
    (install / "keep.bin").write_bytes(b"BASE KEEP")  # same length as b"base keep"

    backend.repair()

    assert (install / "keep.bin").read_bytes() == b"base keep"


# ---- #1827: RESUME after a Repair that stopped part-way finishes the Repair ------------------
def _resume(backend, monkeypatch):
    """What RESUME runs (install()), without the prefix and WebView2 set-up."""
    for step in ("_ensure_default_runner", "_ensure_direct_prefix", "_ensure_sign_in_runtime"):
        monkeypatch.setattr(backend, step, lambda *a, **k: None)
    backend.install()


def test_resume_after_a_stopped_repair_fetches_the_full_package(publisher, tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    install = _delta_install(publisher, backend)
    (install / "keep.bin").write_bytes(b"damaged")
    backend._write_install_marker(("3.9.1", "3.9.1"))  # a Repair stopped after it changed files
    publisher.calls.clear()

    _resume(backend, monkeypatch)

    resources = [body for url, body in publisher.calls if url.endswith("/resource")]
    assert resources and all(body["currentVersion"] == "" for body in resources)
    assert (install / "keep.bin").read_bytes() == b"base keep"
    assert not backend._install_incomplete()


def test_resume_without_a_version_in_the_marker_fetches_the_full_package(
    publisher, tmp_path, monkeypatch
):
    backend = _backend(tmp_path)
    install = _delta_install(publisher, backend)
    (install / "keep.bin").write_bytes(b"damaged")
    backend._write_install_marker()
    publisher.calls.clear()

    _resume(backend, monkeypatch)

    assert (install / "keep.bin").read_bytes() == b"base keep"


def test_resume_after_a_stopped_update_still_takes_the_delta(publisher, tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    install = backend.install_dir()
    dd.sync(
        publisher.plan("3.9.0", {"fullPackage": publisher.base, "diffPackage": None}),
        install,
        workers=1,
    )
    backend._write_version_file("3.9.0", "r1999")
    backend._write_install_marker(("3.9.0", "3.9.1"))
    publisher.calls.clear()

    _resume(backend, monkeypatch)

    resources = [body for url, body in publisher.calls if url.endswith("/resource")]
    assert resources and all(body["currentVersion"] == "3.9.0" for body in resources)
    assert (install / "updated.bin").read_bytes() == b"new"
