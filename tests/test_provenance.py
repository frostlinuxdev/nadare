# SPDX-License-Identifier: GPL-3.0-or-later
"""#1594 item 1: an unsigned in-toto statement with SLSA provenance for a release asset."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "provenance.py"

COMMIT = "4f1c2d9a8b7e6f50123456789abcdef012345678"


@pytest.fixture(scope="module")
def provenance():
    spec = importlib.util.spec_from_file_location("provenance_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def asset(tmp_path):
    path = tmp_path / "Nadare-0.1.0b25-x86_64.AppImage"
    path.write_bytes(b"not really an AppImage\n")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_statement_has_the_in_toto_and_slsa_shape(provenance, asset):
    statement = provenance.build_statement(
        asset, commit=COMMIT, command="scripts/build_appimage.sh"
    )
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    assert statement["subject"] == [
        {"name": asset.name, "digest": {"sha256": _sha256(asset)}}
    ]
    predicate = statement["predicate"]
    assert isinstance(predicate["buildDefinition"]["buildType"], str)
    assert predicate["buildDefinition"]["externalParameters"]["command"] == (
        "scripts/build_appimage.sh"
    )
    assert {
        "digest": {"gitCommit": COMMIT}
    } in predicate["buildDefinition"]["resolvedDependencies"]
    assert predicate["runDetails"]["builder"]["id"] == "nadare-release-scripts"


def test_the_subject_digest_matches_the_file(provenance, asset):
    statement = provenance.build_statement(asset, commit=COMMIT)
    assert statement["subject"][0]["digest"]["sha256"] == hashlib.sha256(
        asset.read_bytes()
    ).hexdigest()


def test_the_commit_is_normalised_to_lower_case(provenance, asset):
    statement = provenance.build_statement(asset, commit=COMMIT.upper())
    assert (
        statement["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"][
            "gitCommit"
        ]
        == COMMIT
    )


def test_an_invalid_commit_is_refused(provenance, asset):
    with pytest.raises(ValueError, match="commit"):
        provenance.build_statement(asset, commit="deadbeef")


def test_the_timestamp_comes_from_source_date_epoch(provenance, asset, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    statement = provenance.build_statement(asset, commit=COMMIT)
    metadata = statement["predicate"]["runDetails"]["metadata"]
    assert metadata["startedOn"] == "2023-11-14T22:13:20Z"
    assert metadata["finishedOn"] == "2023-11-14T22:13:20Z"
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    statement = provenance.build_statement(asset, commit=COMMIT)
    assert "startedOn" not in statement["predicate"]["runDetails"]["metadata"]


def test_two_runs_write_byte_identical_files(
    provenance, asset, tmp_path, monkeypatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = tmp_path / "first.intoto.json"
    second = tmp_path / "second.intoto.json"
    for output in (first, second):
        provenance.write_statement(asset, output, commit=COMMIT, command="build")
    assert first.read_bytes() == second.read_bytes()


def test_the_cli_writes_the_requested_file(provenance, asset, tmp_path):
    output = tmp_path / "asset.intoto.json"
    argv = [
        str(asset),
        "--commit",
        COMMIT,
        "--command",
        "scripts/build_appimage.sh",
        "--out",
        str(output),
    ]
    assert provenance.main(argv) == 0
    statement = json.loads(output.read_text(encoding="utf-8"))
    assert statement["subject"][0]["digest"]["sha256"] == _sha256(asset)
    assert statement["predicate"]["runDetails"]["builder"]["id"] == (
        "nadare-release-scripts"
    )
    assert output.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_output_cannot_overwrite_the_asset(provenance, asset, tmp_path, alias):
    output = asset
    if alias != "same":
        output = tmp_path / "output.intoto.json"
        if alias == "symlink":
            output.symlink_to(asset)
        else:
            output.hardlink_to(asset)
    original = asset.read_bytes()
    with pytest.raises(ValueError, match="asset"):
        provenance.write_statement(asset, output, commit=COMMIT, command="build")
    assert asset.read_bytes() == original


@pytest.mark.parametrize("epoch", ["not-an-integer", "9" * 100])
def test_invalid_epoch_is_a_clean_cli_error(
    provenance, asset, tmp_path, monkeypatch, capsys, epoch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
    output = tmp_path / "bad-time.intoto.json"
    with pytest.raises(SystemExit) as error:
        provenance.main([str(asset), "--commit", COMMIT, "--out", str(output)])
    assert error.value.code == 1
    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err
    assert not output.exists()
